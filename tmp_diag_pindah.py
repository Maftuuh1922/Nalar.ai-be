# -*- coding: utf-8 -*-
"""Diagnosa: compile dokumen repro, bandingkan halaman demi halaman dengan PDF asli.
Cari (1) teks yang meluber ke halaman lain, (2) teks yang seharusnya center/justify."""
import io
import json
import sys
import urllib.error
import urllib.request

import fitz

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8087/api/v1"
_CK = ""


def req_plain(method, path, data=None, timeout=60):
    url = BASE + path
    body = json.dumps(data).encode() if data is not None else None
    h = {"Accept": "application/json"}
    if body is not None:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            sc = resp.headers.get_all("Set-Cookie") or [""]
            return resp.status, resp.read(), sc[0]
    except urllib.error.HTTPError as e:
        sc = e.headers.get_all("Set-Cookie") if e.headers else [""]
        return e.code, e.read(), sc[0]


def req(method, path, data=None, timeout=400):
    url = BASE + path
    body = json.dumps(data).encode() if data is not None else None
    h = {"Accept": "application/json", "Cookie": _CK}
    if body is not None:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


st, _, ck = req_plain("POST", "/auth/login", {"username": "debugger2", "password": "debug1234"})
_CK = ck.split(";")[0]

doc_id = "bf181917-d402-4be8-85f3-e9de81d201e9"
st, body = req("GET", f"/co_writer/documents/{doc_id}")
content = json.loads(body)["content"]
print("content:", len(content), "chars")

st, pdf_out = req("POST", f"/co_writer/documents/{doc_id}/compile",
                  {"content": content, "path": "main.tex"})
print("compile:", st, len(pdf_out) // 1024, "KB")

asli = fitz.open(r"uploads/bf181917-d402-4be8-85f3-e9de81d201e9/source/original.pdf")
hasil = fitz.open(stream=pdf_out, filetype="pdf")
print(f"halaman: asli={len(asli)} hasil={len(hasil)}")

# Fingerprint teks per halaman (baris pertama yang unik) untuk melacak pergeseran
def fingerprint(doc):
    out = []
    for pno in range(len(doc)):
        teks = doc[pno].get_text().strip()
        lines = [l.strip() for l in teks.splitlines() if l.strip()]
        # cari baris kalimat pertama yang cukup panjang & unik
        unik = ""
        for l in lines:
            if len(l) > 30:
                unik = l[:60]
                break
        out.append((pno + 1, unik, len(teks)))
    return out

fa = fingerprint(asli)
fh = fingerprint(hasil)
print("\n=== pemetaan halaman (asli -> kalimat pertama -> hasil) ===")
for a in fa[:16]:
    # cari di hasil
    cocok = [h[0] for h in fh if h[1] and h[1] == a[1]]
    print(f"asli h{a[0]:>2}: {a[1][:52]!r:56} -> hasil h{cocok if cocok else '?'}")

asli.close()
hasil.close()
