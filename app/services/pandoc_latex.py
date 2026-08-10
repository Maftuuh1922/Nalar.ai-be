"""Lapis 3 (PRD v2.8 §4.2) — export LaTeX via Pandoc + template kampus.

Menggantikan generator regex custom: escaping karakter, tabel (termasuk
multi-baris), daftar bersarang, dan struktur heading ditangani Pandoc yang
matang. Sitasi [n] tetap ditangani sistem sendiri (bukan Pandoc) — lihat
citation_formatter — karena Nalar AI perlu kontrol penuh atas mapping nomor
referensi per format.

Template `ulbi-template.tex` memisahkan aturan tampilan (margin, font, spasi)
dari konten: template jarang berubah, konten sering berubah.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

_PANDOC = r"C:\Users\Administrator\Documents\project ta\bin\pandoc.exe"
_TEMPLATE = os.path.join(
    os.path.dirname(__file__), "templates", "ulbi-template.tex"
)

_HEADING = re.compile(r"\\(section|subsection|subsubsection)\{")


def _tersedia() -> bool:
    return os.path.exists(_PANDOC)


def _gambar_url_ke_lokal(url: str, asset_dirs: list[str]) -> str:
    """URL /uploads/... → path lokal absolut (tectonic tidak bisa fetch http)."""
    m = re.search(r"/uploads/[^?)\"]+/([^?)\"/]+)", url)
    if not m:
        return url
    for base in asset_dirs:
        kandidat = os.path.join(base, m.group(1))
        if os.path.exists(kandidat):
            return os.path.abspath(kandidat).replace("\\", "/")
    return url


def markdown_to_latex_pandoc(markdown_text: str, *, title: str = "") -> str:
    """Markdown → source LaTeX via Pandoc + template kampus (ULBI).

    Heading dinomor ulang jadi `\section*` (tanpa nomor otomatis): nomor bab
    dan sub-bab sudah tertulis di teks heading hasil impor ("1.1 Latar
    Belakang"), sehingga nomor otomatis Pandoc justru membuat dobel.
    """
    if not _tersedia():
        raise RuntimeError("Pandoc tidak ditemukan di bin/pandoc.exe.")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(markdown_text or "")
        md_path = fh.name
    try:
        cmd = [
            _PANDOC, md_path, "-f", "markdown", "-t", "latex",
            "--template", _TEMPLATE,
        ]
        if title:
            cmd += ["-M", f"title={title}"]
        hasil = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", timeout=120
        )
    finally:
        try:
            os.remove(md_path)
        except OSError:
            pass
    if hasil.returncode != 0:
        # Kode keluar disertakan karena pandoc yang gagal dijalankan (memori
        # sistem habis) tidak menulis apa pun ke stdout/stderr; tanpa kodenya
        # pesan galatnya kosong dan tak bisa dibedakan dari galat sintaks.
        rincian = hasil.stderr.strip()[:300] or hasil.stdout.strip()[:300]
        raise RuntimeError(
            f"Pandoc gagal (kode {hasil.returncode}): "
            + (rincian or "tanpa keluaran log — biasanya memori sistem habis.")
        )
    tex = hasil.stdout
    # Heading tanpa nomor otomatis LaTeX (nomornya sudah di teks heading).
    tex = _HEADING.sub(lambda m: f"\\{m.group(1)}*{{", tex)
    return tex


def pandoc_to_pdf(
    markdown_text: str,
    output_path: str,
    *,
    asset_dirs: list[str] | None = None,
    jobname: str = "laporan",
) -> str:
    """Markdown → PDF final: Pandoc → LaTeX → tectonic.

    Mengembalikan path PDF. Melempar RuntimeError bila salah satu tahap gagal
    (dipakai sebagai tanda fallback chain §4.3).
    """
    asset_dirs = asset_dirs or []
    # Gambar URL /uploads/... tidak bisa di-fetch tectonic — rewrite ke lokal.
    md = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        lambda m: f"![{m.group(1)}]({_gambar_url_ke_lokal(m.group(2), asset_dirs)})",
        markdown_text or "",
    )
    tex = markdown_to_latex_pandoc(md)
    import shutil

    with tempfile.TemporaryDirectory() as tmpdir:
        from app.services.latex_export import compile_latex_pdf

        pdf = compile_latex_pdf(
            tex, tmpdir, jobname=jobname, asset_dirs=asset_dirs
        )
        shutil.copyfile(pdf, output_path)
    return output_path
