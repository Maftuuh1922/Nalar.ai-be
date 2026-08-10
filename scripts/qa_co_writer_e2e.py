"""Uji rantai penuh Co-Writer terhadap peladen hidup: impor DOCX, tulis dengan AI,
sitasi, sampai ekspor.

Berbeda dari `qa_co_writer_smoke.py` yang menguji struktur berkas, skrip ini
menguji jalur yang menyentuh dunia luar — pengurai DOCX, penyedia LLM, dan
pencarian jurnal — jadi kegagalannya perlu dibedakan: cacat kode kita, atau
layanan luar yang tak tersedia.
"""

from __future__ import annotations

import io
import os
import re
import sys
import time
from pathlib import Path

import httpx
from pypdf import PdfReader

# Peladen tujuan. Port 8087 adalah instance yang benar-benar dipakai aplikasi;
# instance uji terpisah bisa ditunjuk lewat QA_BASE_PORT tanpa mengubah skrip.
BASE = f"http://127.0.0.1:{os.getenv('QA_BASE_PORT', '8087')}/api/v1"
USER = {"username": "qa_filetree@test.local", "password": "QaFileTree123!"}
DOCX = Path(r"C:\Users\Administrator\Documents\project ta\Laporan_Tugas_Akhir_Nalar_AI.docx")

lolos: list[str] = []
gagal: list[str] = []
lewat: list[str] = []


def cek(nama: str, syarat: bool, catatan: str = "") -> bool:
    (lolos if syarat else gagal).append(nama)
    print(f"[{'PASS' if syarat else 'FAIL'}] {nama}" + (f" — {catatan}" if catatan else ""))
    return syarat


def lewati(nama: str, alasan: str) -> None:
    lewat.append(nama)
    print(f"[SKIP] {nama} — {alasan}")


def halaman(pdf: bytes) -> int:
    if pdf[:4] != b"%PDF":
        return 0
    return len(PdfReader(io.BytesIO(pdf)).pages)


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=600.0) as c:
        if not cek("login", c.post("/auth/login", json=USER).status_code == 200):
            return 1

        # ============ 1. Impor DOCX ============
        t = time.time()
        with DOCX.open("rb") as fh:
            r = c.post(
                "/co_writer/import-file",
                files={"file": (DOCX.name, fh, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            )
        durasi_impor = time.time() - t
        if not cek(
            "impor DOCX",
            r.status_code in (200, 201),
            f"{r.status_code}, {durasi_impor:.1f}s, {r.text[:160]}",
        ):
            return 1
        hasil = r.json()
        doc_id = hasil.get("id") or hasil.get("document_id") or (hasil.get("document") or {}).get("id")
        if not cek("impor mengembalikan id dokumen", bool(doc_id), str(list(hasil))):
            return 1
        d = f"/co_writer/documents/{doc_id}"

        try:
            detail = c.get(d).json()
            isi = detail.get("content") or ""
            cek(
                "hasil impor berupa LaTeX",
                detail.get("content_format") == "latex" and "\\documentclass" in isi,
                f"format={detail.get('content_format')}, {len(isi)} aksara",
            )
            cek("isi impor tidak kosong", len(isi) > 5000, f"{len(isi)} aksara")
            # Hanya tebal Markdown BERPASANGAN yang dihitung: satu bintang ganda
            # tunggal memang ada di naskah aslinya dan bukan penanda format.
            tebal = re.findall(r"\*\*[^*\n]{1,80}\*\*", isi)
            cek(
                "impor tidak meninggalkan sisa Markdown",
                "\n# " not in isi and not tebal,
                f"'# '={isi.count(chr(10) + '# ')}, '**tebal**'={len(tebal)}",
            )

            r = c.get(f"{d}/outline")
            heading = (r.json() or {}).get("headings") or []
            cek("outline dokumen impor", len(heading) >= 5, f"{len(heading)} heading")

            # ============ 2. Kompilasi PDF hasil impor ============
            t = time.time()
            r = c.post(f"{d}/compile", json={})
            pdf = r.content if r.status_code == 200 else b""
            cek(
                "kompilasi PDF dokumen impor",
                halaman(pdf) > 0,
                f"{r.status_code}, {halaman(pdf)} halaman, {time.time() - t:.1f}s"
                + ("" if r.status_code == 200 else f", {r.text[:200]}"),
            )

            # ============ 3. AI: perbaiki kalimat ============
            t = time.time()
            r = c.post(
                "/co_writer/edit",
                json={
                    "text": "penelitian ini bikin aplikasi buat nulis skripsi pake AI.",
                    "instruction": "Perbaiki menjadi gaya akademik formal.",
                    "action": "rewrite",
                },
            )
            durasi_ai = time.time() - t
            if r.status_code == 200:
                keluar = (r.json() or {}).get("edited_text", "")
                cek(
                    "AI edit menghasilkan LaTeX bersih",
                    len(keluar) > 20 and "```" not in keluar and "**" not in keluar,
                    f"{durasi_ai:.1f}s — {keluar[:120]!r}",
                )
            elif r.status_code in (502, 504):
                lewati("AI edit", f"penyedia model {r.status_code} setelah {durasi_ai:.1f}s: {r.text[:120]}")
            else:
                cek("AI edit", False, f"{r.status_code} {r.text[:200]}")

            # ============ 4. AI: auto-struktur ============
            t = time.time()
            r = c.post(
                "/co_writer/automark",
                json={"text": "Pendahuluan\nLatar belakang penelitian.\nMetodologi\nPendekatan kuantitatif."},
            )
            if r.status_code == 200:
                keluar = (r.json() or {}).get("marked_text") or (r.json() or {}).get("text", "")
                cek(
                    "AI automark memberi perintah LaTeX",
                    "\\section" in keluar or "\\subsection" in keluar,
                    f"{time.time() - t:.1f}s — {keluar[:120]!r}",
                )
            elif r.status_code in (502, 504):
                lewati("AI automark", f"penyedia model {r.status_code}")
            else:
                cek("AI automark", False, f"{r.status_code} {r.text[:200]}")

            # ============ 5. AI: chat dengan dokumen ============
            t = time.time()
            r = c.post(f"{d}/chat", json={"message": "Sebutkan satu bagian yang masih kosong di draf ini."})
            if r.status_code == 200:
                balas = (r.json() or {}).get("reply") or (r.json() or {}).get("answer") or ""
                # Pesan cadangan dihitung GAGAL: statusnya 200 dan panjangnya
                # lewat ambang, jadi pemeriksaan panjang saja meloloskan balasan
                # kosong yang sebenarnya berarti model tak menghasilkan apa pun.
                cek(
                    "AI chat dokumen",
                    len(balas) > 10 and "belum memperoleh jawaban" not in balas,
                    f"{time.time() - t:.1f}s — {balas[:120]!r}",
                )
            elif r.status_code in (502, 504):
                lewati("AI chat dokumen", f"penyedia model {r.status_code}")
            else:
                cek("AI chat dokumen", False, f"{r.status_code} {r.text[:200]}")

            # ============ 6. Sitasi: bibliografi dari penanda [n] ============
            r = c.get(d)
            isi_kini = (r.json() or {}).get("content") or ""
            punya_penanda = "[1]" in isi_kini
            t = time.time()
            r = c.post(f"{d}/regenerate-bibliography", json={})
            if r.status_code == 200:
                data = r.json() or {}
                jumlah = data.get("citation_count")
                bib = data.get("bibliography") or ""
                cek(
                    "generate bibliografi",
                    isinstance(jumlah, int) and (jumlah == 0 or "Daftar Pustaka" in bib),
                    f"{time.time() - t:.1f}s, {jumlah} sitasi, penanda [n] di draf={punya_penanda}",
                )
                isi_baru = (c.get(d).json() or {}).get("content") or ""
                cek(
                    "bibliografi disisipkan sebelum \\end{document}",
                    jumlah == 0
                    or (
                        "NALAR-AI:BIBLIOGRAPHY:START" in isi_baru
                        and isi_baru.index("NALAR-AI:BIBLIOGRAPHY:START") < isi_baru.index("\\end{document}")
                    ),
                    f"{len(isi_baru)} aksara",
                )
                # Idempoten: dijalankan dua kali tidak boleh menumpuk.
                c.post(f"{d}/regenerate-bibliography", json={})
                isi_dua = (c.get(d).json() or {}).get("content") or ""
                cek(
                    "generate bibliografi tidak menduplikasi",
                    isi_dua.count("NALAR-AI:BIBLIOGRAPHY:START") <= 1,
                    f"{isi_dua.count('NALAR-AI:BIBLIOGRAPHY:START')} blok",
                )
            elif r.status_code in (502, 504):
                lewati("generate bibliografi", f"penyedia/pencarian {r.status_code}")
            else:
                cek("generate bibliografi", False, f"{r.status_code} {r.text[:200]}")

            # ============ 7. Sitasi tersimpan (Learning Space) ============
            r = c.get("/co_writer/learning-space")
            cek("learning-space terbaca", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

            # ============ 8. Ekspor laporan ============
            r = c.get(f"{d}/export-docx")
            cek(
                "ekspor DOCX",
                r.status_code == 200 and r.content[:2] == b"PK",
                f"{r.status_code}, {len(r.content)} byte",
            )
            r = c.get(f"{d}/export-latex", params={"format": "tex"})
            cek(
                "ekspor .tex",
                r.status_code == 200 and "\\documentclass" in r.text,
                f"{r.status_code}, {len(r.text)} aksara",
            )
            r = c.get(f"{d}/export-latex", params={"format": "pdf"})
            cek(
                "ekspor PDF typeset",
                r.status_code == 200 and halaman(r.content) > 0,
                f"{r.status_code}, {halaman(r.content)} halaman",
            )

            # ============ 9. Ronde struktur: pecah per bab lalu ekspor lagi ============
            # Diukur ulang tepat sebelum pecah: langkah bibliografi menambah satu
            # \section*{Daftar Pustaka} setelah pengukuran pertama, jadi memakai
            # angka awal akan salah menuduh pendataran \input{} bocor.
            heading_pra = ((c.get(f"{d}/outline").json()) or {}).get("headings") or []
            r = c.post(f"{d}/split", json={})
            if cek("pecah per bab dokumen nyata", r.status_code == 200, r.text[:160]):
                berkas = (c.get(f"{d}/files").json() or {}).get("files") or []
                cek("berkas bab terbentuk", len(berkas) >= 3, f"{len(berkas)} berkas")
                heading_split = ((c.get(f"{d}/outline").json()) or {}).get("headings") or []
                cek(
                    "outline setelah pecah tetap utuh",
                    len(heading_split) == len(heading_pra),
                    f"{len(heading_split)} vs {len(heading_pra)}",
                )
                r = c.post(f"{d}/compile", json={})
                cek(
                    "kompilasi setelah pecah tetap utuh",
                    halaman(r.content) >= max(1, halaman(pdf) - 1),
                    f"{halaman(r.content)} vs {halaman(pdf)} halaman",
                )
        finally:
            c.delete(d)

    print(f"\n{len(lolos)} lolos, {len(gagal)} gagal, {len(lewat)} dilewati")
    if gagal:
        print("Gagal: " + ", ".join(gagal))
    if lewat:
        print("Dilewati: " + ", ".join(lewat))
    return 1 if gagal else 0


if __name__ == "__main__":
    mulai = time.time()
    kode = main()
    print(f"selesai dalam {time.time() - mulai:.1f}s")
    sys.exit(kode)
