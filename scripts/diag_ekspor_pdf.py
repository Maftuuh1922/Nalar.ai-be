"""Bandingkan tiga PDF dari naskah yang sama: asli, /compile, dan /export-latex.

Dipakai untuk melacak laporan pengguna "ekspor PDF hasilnya rusak": yang perlu
dibuktikan bukan bahwa ekspornya jalan (status 200 sudah lama benar) melainkan
bahwa isinya masih setara naskah masukan. Karena itu yang diukur halaman,
jumlah judul, tabel, gambar, dan potongan kalimat — bukan status HTTP.
"""

from __future__ import annotations

import io
import os
import re
import time
from pathlib import Path

import httpx
from pypdf import PdfReader

BASE = f"http://127.0.0.1:{os.getenv('QA_BASE_PORT', '8087')}/api/v1"
USER = {"username": "qa_filetree@test.local", "password": "QaFileTree123!"}
AKAR = Path(r"C:\Users\Administrator\Documents\project ta")
DOCX = AKAR / "Laporan_Tugas_Akhir_Nalar_AI.docx"
PDF_ASLI = AKAR / "Laporan_Tugas_Akhir_Nalar_AI.pdf"


def teks_pdf(data: bytes) -> tuple[int, str]:
    if data[:4] != b"%PDF":
        return 0, ""
    r = PdfReader(io.BytesIO(data))
    return len(r.pages), "\n".join(p.extract_text() or "" for p in r.pages)


def normal(teks: str) -> str:
    return re.sub(r"\s+", " ", teks).strip().lower()


def ringkas(nama: str, data: bytes) -> str:
    hal, teks = teks_pdf(data)
    n = normal(teks)
    return (
        f"{nama:28} {hal:>4} halaman, {len(teks):>7} aksara teks, "
        f"'bab ' x{n.count('bab ')}, 'gambar ' x{n.count('gambar ')}, "
        f"'tabel ' x{n.count('tabel ')}"
    )


def main() -> int:
    print(ringkas("PDF asli (Word)", PDF_ASLI.read_bytes()))

    with httpx.Client(base_url=BASE, timeout=900.0) as c:
        c.post("/auth/login", json=USER)
        with DOCX.open("rb") as fh:
            imp = c.post(
                "/co_writer/import-file",
                files={
                    "file": (
                        DOCX.name,
                        fh,
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document",
                    )
                },
            ).json()
        doc_id = imp["id"]
        d = f"/co_writer/documents/{doc_id}"
        try:
            detail = c.get(d).json()
            tex = detail.get("content") or ""
            print(
                f"{'sumber LaTeX tersimpan':28} {len(tex):>7} aksara, "
                f"section*={tex.count(chr(92) + 'section*{')}, "
                f"section={tex.count(chr(92) + 'section{')}, "
                f"textbf={tex.count(chr(92) + 'textbf{')}, "
                f"newpage={tex.count(chr(92) + 'newpage')}, "
                f"includegraphics={tex.count('includegraphics')}, "
                f"tabular={tex.count('begin{tabular}')}, "
                f"structured_content={'ada' if detail.get('structured_content') else 'tidak'}"
            )

            t0 = time.time()
            r = c.post(f"{d}/compile", json={})
            print(f"\n/compile        -> {r.status_code}, {time.time() - t0:.1f}s")
            if r.status_code == 200:
                print(ringkas("  hasil /compile", r.content))
                Path("diag_compile.pdf").write_bytes(r.content)

            t0 = time.time()
            r = c.get(f"{d}/export-latex", params={"format": "pdf"})
            fb = r.headers.get("x-fallback-notice")
            print(
                f"\n/export-latex?pdf -> {r.status_code}, {time.time() - t0:.1f}s, "
                f"tipe={r.headers.get('content-type')}"
            )
            if fb:
                print(f"  FALLBACK: {fb}")
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                print(ringkas("  hasil ekspor", r.content))
                Path("diag_ekspor.pdf").write_bytes(r.content)

            # Sumber .tex yang diunduh: pembanding untuk melihat apakah
            # kerusakan terjadi sebelum atau sesudah kompilasi.
            r = c.get(f"{d}/export-latex", params={"format": "tex"})
            if r.status_code == 200:
                Path("diag_ekspor.tex").write_text(r.text, encoding="utf-8")
                print(f"\nsumber .tex terunduh: {len(r.text)} aksara")
        finally:
            c.delete(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
