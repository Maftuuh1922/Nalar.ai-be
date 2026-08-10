"""Uji asap (smoke test) alur proyek multi-berkas Co-Writer lewat HTTP.

Dijalankan terhadap peladen yang benar-benar hidup, karena yang ingin dibuktikan
di sini bukan logika fungsinya (itu sudah diuji pytest) melainkan bahwa rute,
skema basis data, dan kompilasi tectonic saling tersambung.
"""

from __future__ import annotations

import io
import sys
import time

import httpx
from pypdf import PdfReader


def jumlah_halaman(pdf: bytes) -> int:
    """Hitung halaman sungguhan. Menghitung '/Type /Page' pada byte mentah tidak
    bisa dipakai: tectonic memampatkan katalog objeknya."""
    if pdf[:4] != b"%PDF":
        return 0
    return len(PdfReader(io.BytesIO(pdf)).pages)

BASE = "http://127.0.0.1:8087/api/v1"
USER = {"username": "qa_filetree@test.local", "password": "QaFileTree123!"}

NASKAH = r"""\documentclass[12pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\begin{document}

\section{Pendahuluan}
Latar belakang penelitian ini adalah kebutuhan struktur berkas.

\section{Tinjauan Pustaka}
Kajian terdahulu menunjukkan hal serupa.

\section{Metodologi}
Penelitian memakai pendekatan rekayasa perangkat lunak.

\end{document}
"""

lolos: list[str] = []
gagal: list[str] = []


def cek(nama: str, syarat: bool, catatan: str = "") -> None:
    (lolos if syarat else gagal).append(nama)
    tanda = "PASS" if syarat else "FAIL"
    print(f"[{tanda}] {nama}" + (f" — {catatan}" if catatan else ""))


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=180.0) as c:
        r = c.post("/auth/login", json=USER)
        cek("login", r.status_code == 200, str(r.status_code))
        if r.status_code != 200:
            return 1

        r = c.post(
            "/co_writer/documents",
            json={"title": "QA Proyek Multi-Berkas", "content": NASKAH, "content_format": "latex"},
        )
        cek("buat dokumen", r.status_code in (200, 201), str(r.status_code))
        doc_id = r.json().get("id")
        if not doc_id:
            print(r.text[:500])
            return 1
        d = f"/co_writer/documents/{doc_id}"

        try:
            # --- Dasar: outline & kompilasi sebelum dipecah ---
            r = c.get(f"{d}/outline")
            outline_awal = (r.json() or {}).get("headings") or []
            cek("outline sebelum split", len(outline_awal) == 3, f"{len(outline_awal)} heading")

            r = c.post(f"{d}/compile", json={})
            pdf_awal = r.content if r.status_code == 200 else b""
            cek(
                "compile sebelum split",
                r.status_code == 200 and pdf_awal[:4] == b"%PDF",
                f"{r.status_code}, {len(pdf_awal)} byte",
            )
            halaman_awal = jumlah_halaman(pdf_awal)

            # --- Split per bab ---
            r = c.post(f"{d}/split", json={})
            cek("split per bab", r.status_code == 200, r.text[:200])
            berkas_split = (r.json() or {}).get("files") or []
            cek("split menghasilkan 3 berkas", len(berkas_split) == 3, str(berkas_split))

            r = c.get(f"{d}/files")
            daftar = r.json()
            daftar = daftar if isinstance(daftar, list) else daftar.get("files", [])
            jalur = sorted(f["path"] for f in daftar)
            cek("daftar berkas", len(jalur) >= 3, str(jalur))

            r = c.get(d)
            main_tex = r.json().get("content") or ""
            cek(
                "main.tex tinggal preamble + input",
                "\\input{" in main_tex and "Latar belakang" not in main_tex,
                f"{len(main_tex)} aksara",
            )

            # --- Pendataran: outline & kompilasi setelah dipecah ---
            r = c.get(f"{d}/outline")
            outline_akhir = (r.json() or {}).get("headings") or []
            cek(
                "outline setelah split tetap sama",
                len(outline_akhir) == len(outline_awal),
                f"{len(outline_akhir)} vs {len(outline_awal)}",
            )
            cek(
                "outline menunjuk berkas bab",
                all(h.get("path", "").startswith("bab/") for h in outline_akhir),
                str([h.get("path") for h in outline_akhir]),
            )

            r = c.post(f"{d}/compile", json={})
            pdf_akhir = r.content if r.status_code == 200 else b""
            halaman_akhir = jumlah_halaman(pdf_akhir)
            cek(
                "compile setelah split",
                r.status_code == 200 and pdf_akhir[:4] == b"%PDF",
                f"{r.status_code}, {len(pdf_akhir)} byte",
            )
            cek(
                "jumlah halaman PDF tidak menyusut",
                halaman_akhir >= halaman_awal > 0,
                f"{halaman_akhir} vs {halaman_awal}",
            )

            # --- CRUD berkas ---
            bab_pertama = next((p for p in jalur if p.startswith("bab/")), None)
            r = c.get(f"{d}/files/{bab_pertama}")
            isi_bab = (r.json() or {}).get("content", "")
            cek("baca berkas bab", r.status_code == 200 and "\\section" in isi_bab, str(r.status_code))

            r = c.put(f"{d}/files/{bab_pertama}", json={"content": isi_bab + "\nKalimat tambahan QA.\n"})
            cek("simpan berkas bab", r.status_code == 200, r.text[:160])
            r = c.get(f"{d}/files/{bab_pertama}")
            cek("isi tersimpan", "Kalimat tambahan QA." in r.json().get("content", ""))

            r = c.put(f"{d}/files/references.bib", json={"content": "@article{a2024, title={Uji}}\n"})
            cek("buat references.bib", r.status_code == 200, r.text[:160])

            r = c.post(f"{d}/files/rename", json={"from": "references.bib", "to": "pustaka.bib"})
            cek("rename berkas", r.status_code == 200, r.text[:160])
            r = c.get(f"{d}/files/pustaka.bib")
            cek("berkas hasil rename terbaca", r.status_code == 200, str(r.status_code))
            r = c.get(f"{d}/files/references.bib")
            cek("nama lama hilang", r.status_code == 404, str(r.status_code))

            r = c.delete(f"{d}/files/pustaka.bib")
            cek("hapus berkas", r.status_code in (200, 204), str(r.status_code))

            # --- Keamanan jalur ---
            r = c.get(f"{d}/files/../../etc/passwd")
            cek("jalur ../ ditolak", r.status_code in (400, 404, 422), str(r.status_code))

            # --- Split kedua harus ditolak ---
            r = c.post(f"{d}/split", json={})
            cek("split ulang ditolak", r.status_code >= 400, str(r.status_code))

            # --- Ekspor ---
            r = c.get(f"{d}/export-latex", params={"format": "tex"})
            tex = r.text if r.status_code == 200 else ""
            cek(
                "export-latex?format=tex mendatarkan input",
                r.status_code == 200 and "Latar belakang" in tex and "\\input{" not in tex,
                f"{r.status_code}, {len(tex)} aksara",
            )

            r = c.get(f"{d}/export-latex", params={"format": "pdf"})
            cek(
                "export-latex?format=pdf",
                r.status_code == 200 and jumlah_halaman(r.content) > 0,
                f"{r.status_code}, {jumlah_halaman(r.content)} halaman",
            )

            r = c.get(f"{d}/export-docx")
            cek(
                "export-docx",
                r.status_code == 200 and r.content[:2] == b"PK",
                f"{r.status_code}, {len(r.content)} byte",
            )

            # /md tanpa `path` sengaja membaca main.tex saja (dipasangkan dengan
            # /from-md yang menulis balik ke main.tex); isi bab dibaca lewat `path`.
            r = c.get(f"{d}/md")
            md_utama = (r.json() or {}).get("markdown", "") if r.status_code == 200 else ""
            cek(
                "md tanpa path = main.tex saja",
                r.status_code == 200 and "Latar belakang" not in md_utama,
                f"{r.status_code}, {len(md_utama)} aksara",
            )
            r = c.get(f"{d}/md", params={"path": bab_pertama})
            md_bab = (r.json() or {}).get("markdown", "") if r.status_code == 200 else ""
            cek(
                "md dengan path = isi bab",
                r.status_code == 200 and "Latar belakang" in md_bab,
                f"{r.status_code}, {len(md_bab)} aksara",
            )

            r = c.get(f"{d}/gap-analysis")
            cek("gap-analysis", r.status_code == 200, r.text[:160])

            # --- Checkpoint dari split ---
            r = c.get(f"{d}/checkpoints")
            cps = (r.json() or {}).get("checkpoints") or []
            cek(
                "checkpoint split tercatat",
                any("pecah" in (cp.get("label") or "").lower() for cp in cps),
                str([cp.get("label") for cp in cps]),
            )

        finally:
            c.delete(d)

    print(f"\n{len(lolos)} lolos, {len(gagal)} gagal")
    if gagal:
        print("Gagal: " + ", ".join(gagal))
    return 1 if gagal else 0


if __name__ == "__main__":
    mulai = time.time()
    kode = main()
    print(f"selesai dalam {time.time() - mulai:.1f}s")
    sys.exit(kode)
