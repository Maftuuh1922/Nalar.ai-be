"""Uji terfokus jalur kompilasi: impor DOCX → PDF, pecah per bab → PDF.

Dipisahkan dari `qa_co_writer_e2e.py` karena langkah kompilasi paling rentan
gagal bukan karena kode kita: tectonic memuat bundel TeX besar, dan pada mesin
dengan memori sisa di bawah ~1,5 GB ia mati tanpa menulis log sama sekali.
Skrip ini mencatat memori tersedia sebelum tiap kompilasi supaya kegagalan
lingkungan bisa dibedakan dari cacat kode.
"""

from __future__ import annotations

import ctypes
import io
import os
import time
from pathlib import Path

import httpx
from pypdf import PdfReader

# Sama seperti skrip QA lain: bawaan menunjuk instance yang dipakai aplikasi.
BASE = f"http://127.0.0.1:{os.getenv('QA_BASE_PORT', '8087')}/api/v1"
USER = {"username": "qa_filetree@test.local", "password": "QaFileTree123!"}
DOCX = Path(r"C:\Users\Administrator\Documents\project ta\Laporan_Tugas_Akhir_Nalar_AI.docx")


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def memori_bebas_gb() -> float:
    m = _MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return round(m.ullAvailPhys / 2**30, 2)


def halaman(pdf: bytes) -> int:
    if pdf[:4] != b"%PDF":
        return 0
    return len(PdfReader(io.BytesIO(pdf)).pages)


def main() -> int:
    gagal = 0
    with httpx.Client(base_url=BASE, timeout=900.0) as c:
        c.post("/auth/login", json=USER)
        with DOCX.open("rb") as fh:
            imp = c.post(
                "/co_writer/import-file",
                files={
                    "file": (
                        DOCX.name,
                        fh,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            ).json()
        d = f"/co_writer/documents/{imp['id']}"
        try:
            for label, aksi in (
                ("kompilasi utuh", lambda: c.post(f"{d}/compile", json={})),
                (
                    "ekspor PDF typeset",
                    lambda: c.get(f"{d}/export-latex", params={"format": "pdf"}),
                ),
                ("pecah per bab", lambda: c.post(f"{d}/split", json={})),
                ("kompilasi setelah pecah", lambda: c.post(f"{d}/compile", json={})),
            ):
                bebas = memori_bebas_gb()
                t = time.time()
                r = aksi()
                n = halaman(r.content) if r.headers.get("content-type", "").startswith(
                    "application/pdf"
                ) else -1
                catatan = f"{n} halaman" if n >= 0 else r.text[:180]
                ok = r.status_code == 200 and (n != 0)
                gagal += not ok
                print(
                    f"[{'PASS' if ok else 'FAIL'}] {label} — {r.status_code}, "
                    f"{catatan}, {time.time() - t:.1f}s, RAM bebas sebelum {bebas} GB"
                )
        finally:
            c.delete(d)
    print(f"\n{'semua lolos' if not gagal else f'{gagal} gagal'}")
    return 1 if gagal else 0


if __name__ == "__main__":
    raise SystemExit(main())
