"""Konversi markdown → DOCX dengan sitasi aktif (hyperlink DOI) & PDF.

DOCX:
- Heading #..####, bold **text**, italic *text*, daftar - / 1.
- Sitasi [n] diubah menjadi hyperlink ke DOI (bila tersedia) + penanda
  teks biru bergaris bawah (aktif di Word).
- Daftar pustaka (## Daftar Pustaka) dirender rapi dengan paragraf biasa.

PDF:
- markdown2 + xhtml2pdf → A4 dengan heading/tabel/blockquote rapi.
"""

import re

from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# Biru standar hyperlink Word
LINK_COLOR = RGBColor(0x05, 0x63, 0xC1)


def _add_hyperlink(paragraph, url: str, text: str) -> None:
    """Tambahkan hyperlink asli (aktif di Word) ke paragraf."""
    part = paragraph.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    # Warna biru
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    rPr.append(color)
    # Underline
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rPr.append(u)
    new_run.append(rPr)
    t = OxmlElement("w:t")
    t.text = text
    new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def _split_cite_run(paragraph, text: str, references: dict[int, str], *, bold, italic) -> None:
    """Tulis teks biasa + sitasi [n] → hyperlink DOI bila ada."""
    pos = 0
    for m in re.finditer(r"\[(\d+)\]", text):
        if m.start() > pos:
            run = paragraph.add_run(text[pos : m.start()])
            run.bold = bold
            run.italic = italic
        n = int(m.group(1))
        doi_url = references.get(n)
        if doi_url:
            _add_hyperlink(paragraph, doi_url, f"[{n}]")
        else:
            run = paragraph.add_run(f"[{n}]")
            run.bold = bold
            run.italic = italic
        pos = m.end()
    if pos < len(text):
        run = paragraph.add_run(text[pos:])
        run.bold = bold
        run.italic = italic


def _split_italic(paragraph, text: str, references: dict[int, str], *, bold: bool) -> None:
    """Proses *italic* di dalam teks; sisanya (termasuk sitasi) jadi run biasa."""
    pos = 0
    for m in re.finditer(r"\*(.+?)\*", text, re.S):
        if m.start() > pos:
            _split_cite_run(paragraph, text[pos : m.start()], references, bold=bold, italic=False)
        _split_cite_run(paragraph, m.group(1), references, bold=bold, italic=True)
        pos = m.end()
    if pos < len(text):
        _split_cite_run(paragraph, text[pos:], references, bold=bold, italic=False)


def _split_bold(paragraph, text: str, references: dict[int, str]) -> None:
    """Proses **bold**; isinya boleh memuat *italic* lagi."""
    pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*", text, re.S):
        if m.start() > pos:
            _split_italic(paragraph, text[pos : m.start()], references, bold=False)
        _split_italic(paragraph, m.group(1), references, bold=True)
        pos = m.end()
    if pos < len(text):
        _split_italic(paragraph, text[pos:], references, bold=False)


def _split_inline(paragraph, text: str, references: dict[int, str]) -> None:
    """Parse inline markdown (**bold**, *italic*, ***bold italic***) + sitasi.

    Penanda diproses berjenjang: `***tebal miring***` dulu (kalau tidak,
    pasangan `**` memakan dua dari tiga bintang dan menyisakan `*` mentah),
    lalu `**tebal *miring* tebal**` (isinya dipecah lagi pada `*`).
    """
    pos = 0
    for m in re.finditer(r"\*\*\*(.+?)\*\*\*", text, re.S):
        if m.start() > pos:
            _split_bold(paragraph, text[pos : m.start()], references)
        _split_italic(paragraph, m.group(1), references, bold=True)
        pos = m.end()
    if pos < len(text):
        _split_bold(paragraph, text[pos:], references)


def markdown_to_docx(markdown_text: str, output_path: str, references: dict[int, str] | None = None):
    """Konversi markdown ke DOCX.

    ``references``: mapping {nomor: doi_url} agar sitasi [n] menjadi
    hyperlink aktif ke DOI. Bila None, sitasi dirender sebagai teks biasa.
    """
    references = references or {}
    document = Document()

    style = document.styles["Normal"]
    font = style.font
    font.name = "Arial"
    font.size = Pt(11)

    lines = markdown_text.split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith("# "):
            heading = document.add_heading(line[2:], level=1)
            heading.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        elif line.startswith("## "):
            document.add_heading(line[3:], level=2)
        elif line.startswith("### "):
            document.add_heading(line[4:], level=3)
        elif line.startswith("#### "):
            document.add_heading(line[5:], level=4)
        elif line.startswith("- ") or line.startswith("* "):
            p = document.add_paragraph(style="List Bullet")
            _split_inline(p, line[2:], references)
        elif re.match(r"^\d+\.\s", line):
            p = document.add_paragraph(style="List Number")
            _split_inline(p, re.sub(r"^\d+\.\s", "", line), references)
        else:
            p = document.add_paragraph()
            _split_inline(p, line, references)

    document.save(output_path)
    return output_path
