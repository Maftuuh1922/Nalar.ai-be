# -*- coding: utf-8 -*-
"""Peta halaman asli->hasil dengan tumpang-tindih kata; cetak teks halaman muka."""
import io
import json
import re
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
st, pdf_out = req("POST", f"/co_writer/documents/{doc_id}/compile",
                  {"content": content, "path": "main.tex"})
asli = fitz.open(r"uploads/bf181917-d402-4be8-85f3-e9de81d201e9/source/original.pdf")
hasil = fitz.open(stream=pdf_out, filetype="pdf")
print(f"asli={len(asli)} hasil={len(hasil)}")


def kata(doc, pno):
    return set(re.findall(r"[a-z0-9]+", doc[pno].get_text().lower()))


def map_hal(doc_a, doc_b):
    out = []
    for i in range(len(doc_a)):
        wa = kata(doc_a, i)
        skor = []
        for j in range(len(doc_b)):
            wb = kata(doc_b, j)
            if not wa or not wb:
                skor.append(0)
                continue
            skor.append(len(wa & wb) / min(len(wa), len(wb)))
        j = max(range(len(skor)), key=skor.__getitem__)
        out.append((i + 1, j + 1, round(skor[j], 2)))
    return out


print("\n=== peta halaman asli -> hasil ===")
for a, b, s in map_hal(asli, hasil):
    mark = "  <-- PERHATIKAN" if b != a else ""
    print(f"h{a:>2} -> h{b:>2} (skor {s}){mark}")

print("\n=== halaman hasil (teks, 2 baris pertama & terakhir) ===")
for pno in range(min(12, len(hasil))):
    t = hasil[pno].get_text().strip().splitlines()
    t = [x.strip() for x in t if x.strip()]
    depan = " | ".join(t[:2])[:90]
    belakang = " | ".join(t[-2:])[:90]
    print(f"h{pno+1:>2}: {depan!r}  ...  {belakang!r}")

asli.close()
hasil.close()
