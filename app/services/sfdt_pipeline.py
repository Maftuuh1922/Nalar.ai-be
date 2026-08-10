"""Pipeline ekspor berbasis SFDT (PRD P1): SFDT → DOCX → Markdown → Pandoc.

Editor ala Word (Syncfusion) menghasilkan SFDT; konversi ke DOCX dilakukan
di browser (``DocumentEditor.saveAsBlob``). Berkas DOCX itu dikirim ke sini,
lalu pandoc mengubahnya kembali menjadi Markdown — sumber netral untuk jalur
ekspor lama (template DOCX kampus + sitasi DOI, LaTeX → tectonic PDF) tanpa
menyentuh kolom ``content`` (LaTeX) yang sudah tidak menjadi sumber kebenaran.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

_PANDOC = r"C:\Users\Administrator\Documents\project ta\bin\pandoc.exe"


def tersedia() -> bool:
    return os.path.exists(_PANDOC)


def docx_to_markdown(docx_bytes: bytes, *, title: str = "") -> str:
    """DOCX (dari Syncfusion) → Markdown GFM via Pandoc.

    Gambar hasil ekstraksi pandoc keluar sebagai data URI — tetap terbaca
    oleh pratinjau/typeset; pipeline ekspor PDF menggantinya per jalur
    gambar lokal bila perlu.
    """
    if not tersedia():
        raise RuntimeError("Pandoc tidak ditemukan di bin/pandoc.exe.")
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "dokumen.docx")
        md_path = os.path.join(tmpdir, "dokumen.md")
        with open(docx_path, "wb") as fh:
            fh.write(docx_bytes)
        cmd = [
            _PANDOC, docx_path, "-f", "docx", "-t", "gfm",
            "--wrap=preserve",
        ]
        if title:
            cmd += ["-M", f"title={title}"]
        hasil = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", timeout=120
        )
        if hasil.returncode != 0:
            raise RuntimeError(
                f"Pandoc gagal mengonversi DOCX: "
                f"{hasil.stderr.strip()[:300] or hasil.stdout.strip()[:300]}"
            )
        markdown = hasil.stdout
        # Pandoc menaruh judul dokumen (documentName) sebagai heading H1 di
        # atas isi; buang bila sama dengan isi kepala yang sebenarnya (hampir
        # selalu untuk dokumen yang dimulai dengan heading sendiri).
        markdown = _buang_judul_dobel(markdown, title)
        # Sitasi [n] di-escape pandoc jadi \[n\] — pulihkan supaya regex
        # sitasi ekspor (\[(\d+)\]) tetap melihatnya.
        markdown = re.sub(r"\\\[(\d+(?:[-,]\d+)*)\\\]", r"[\1]", markdown)
        return markdown


def docx_to_latex(docx_bytes: bytes, *, title: str = "") -> str:
    """DOCX → LaTeX langsung (Pandoc docx → markdown → template kampus)."""
    md = docx_to_markdown(docx_bytes, title=title)
    from app.services.pandoc_latex import markdown_to_latex_pandoc

    return markdown_to_latex_pandoc(md, title=title)


def _buang_judul_dobel(markdown: str, title: str) -> str:
    if not title:
        return markdown
    kepala = markdown.strip()
    baris = kepala.splitlines()
    if not baris:
        return markdown
    if baris[0].lstrip().startswith("# "):
        isi_judul = baris[0].lstrip()[2:].strip()
        if isi_judul.strip().lower() == title.strip().lower():
            markdown = "\n".join(baris[1:]).lstrip()
    return markdown
