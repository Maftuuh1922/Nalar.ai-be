"""Konversi HTML hasil editor catatan (Tiptap) menjadi dokumen .docx.

Editor di frontend menyimpan isi catatan sebagai HTML, bukan markdown, sehingga
formatnya jauh lebih kaya daripada yang bisa ditangani ``markdown_to_docx``:
heading, tebal/miring/garis bawah/coret, sorot, daftar bertingkat, tabel,
tautan, garis pemisah, dan perataan paragraf.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

_ALIGNMENT = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}

# Mark inline yang diwarisi turun ke anak-anak node
_INLINE_TAGS = {"strong", "b", "em", "i", "u", "s", "strike", "del", "mark", "code", "a", "span"}

_MAX_LIST_DEPTH = 4


def _hex_to_rgb(value: str) -> RGBColor | None:
    """Ubah '#rrggbb' / 'rgb(r, g, b)' menjadi RGBColor. None kalau tidak dikenali."""
    if not value:
        return None
    value = value.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    if m:
        raw = m.group(1)
        return RGBColor(int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,[^)]*)?\)", value)
    if m:
        return RGBColor(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _style_prop(tag: Tag, prop: str) -> str | None:
    """Ambil satu properti dari atribut style inline."""
    style = tag.get("style") or ""
    m = re.search(rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", style, re.I)
    return m.group(1).strip() if m else None


def _shade_run(run, color_hex: str) -> None:
    """Beri warna latar (highlight) pada run lewat XML — python-docx tidak punya API-nya."""
    rgb = color_hex.lstrip("#")
    if len(rgb) != 6:
        return
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), rgb)
    run._element.get_or_add_rPr().append(shd)


class _Fmt(dict):
    """Kumpulan format inline yang aktif; disalin turun ke node anak."""

    def child(self, **kwargs) -> "_Fmt":
        new = _Fmt(self)
        new.update(kwargs)
        return new


def _add_runs(paragraph, node, fmt: _Fmt) -> None:
    """Tulis isi ``node`` ke ``paragraph`` sambil membawa format inline."""
    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if not text.strip() and not text.startswith(" "):
                continue
            # Rapatkan whitespace HTML seperti browser
            text = re.sub(r"\s+", " ", text)
            if not text:
                continue
            run = paragraph.add_run(text)
            run.bold = bool(fmt.get("bold"))
            run.italic = bool(fmt.get("italic"))
            run.underline = bool(fmt.get("underline"))
            run.font.strike = bool(fmt.get("strike"))
            if fmt.get("code"):
                run.font.name = "Courier New"
            if fmt.get("font_family"):
                run.font.name = fmt["font_family"]
            if fmt.get("font_size"):
                run.font.size = fmt["font_size"]
            if fmt.get("color"):
                run.font.color.rgb = fmt["color"]
            if fmt.get("link"):
                run.font.color.rgb = RGBColor(0x05, 0x63, 0xC1)
                run.underline = True
            if fmt.get("highlight"):
                _shade_run(run, fmt["highlight"])
            continue

        if not isinstance(child, Tag):
            continue

        name = child.name.lower()

        if name == "br":
            paragraph.add_run().add_break()
            continue
        if name == "img":
            alt = child.get("alt") or child.get("src") or "gambar"
            paragraph.add_run(f"[gambar: {alt}]").italic = True
            continue

        next_fmt = fmt
        if name in ("strong", "b"):
            next_fmt = next_fmt.child(bold=True)
        elif name in ("em", "i"):
            next_fmt = next_fmt.child(italic=True)
        elif name == "u":
            next_fmt = next_fmt.child(underline=True)
        elif name in ("s", "strike", "del"):
            next_fmt = next_fmt.child(strike=True)
        elif name == "code":
            next_fmt = next_fmt.child(code=True)
        elif name == "a":
            next_fmt = next_fmt.child(link=True)
        elif name == "mark":
            color = child.get("data-color") or _style_prop(child, "background-color") or "#FFF2A8"
            rgb = _hex_to_rgb(color)
            hex_fill = f"{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}" if rgb else "FFF2A8"
            next_fmt = next_fmt.child(highlight=hex_fill)

        if name in _INLINE_TAGS:
            family = _style_prop(child, "font-family")
            if family:
                next_fmt = next_fmt.child(font_family=family.split(",")[0].strip().strip("'\""))
            size = _style_prop(child, "font-size")
            if size:
                m = re.match(r"([\d.]+)\s*(px|pt)", size, re.I)
                if m:
                    val = float(m.group(1))
                    pts = val * 0.75 if m.group(2).lower() == "px" else val
                    next_fmt = next_fmt.child(font_size=Pt(round(pts, 1)))
            color = _style_prop(child, "color")
            if color:
                rgb = _hex_to_rgb(color)
                if rgb:
                    next_fmt = next_fmt.child(color=rgb)

        _add_runs(paragraph, child, next_fmt)


def _apply_alignment(paragraph, tag: Tag) -> None:
    align = _style_prop(tag, "text-align")
    if align and align.lower() in _ALIGNMENT:
        paragraph.alignment = _ALIGNMENT[align.lower()]


def _add_hr(document) -> None:
    p = document.add_paragraph()
    p_pr = p._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "999999")
    borders.append(bottom)
    p_pr.append(borders)


def _render_list(document, tag: Tag, ordered: bool, depth: int = 0) -> None:
    depth = min(depth, _MAX_LIST_DEPTH - 1)
    base = "List Number" if ordered else "List Bullet"
    style = base if depth == 0 else f"{base} {depth + 1}"
    for li in tag.find_all("li", recursive=False):
        nested = [c for c in li.find_all(["ul", "ol"], recursive=False)]
        try:
            paragraph = document.add_paragraph(style=style)
        except KeyError:  # style level tidak tersedia di template
            paragraph = document.add_paragraph(style=base)

        # Isi <li> tanpa sub-listnya
        for child in li.children:
            if isinstance(child, Tag) and child.name in ("ul", "ol"):
                continue
            if isinstance(child, Tag) and child.name == "p":
                _add_runs(paragraph, child, _Fmt())
            elif isinstance(child, NavigableString):
                text = re.sub(r"\s+", " ", str(child))
                if text.strip():
                    paragraph.add_run(text)
            elif isinstance(child, Tag):
                _add_runs(paragraph, BeautifulSoup(str(child), "html.parser"), _Fmt())

        for sub in nested:
            _render_list(document, sub, sub.name == "ol", depth + 1)


def _render_table(document, tag: Tag) -> None:
    rows = tag.find_all("tr")
    if not rows:
        return
    n_cols = max(len(r.find_all(["td", "th"])) for r in rows)
    table = document.add_table(rows=0, cols=n_cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    for tr in rows:
        cells_html = tr.find_all(["td", "th"])
        row = table.add_row()
        for idx in range(n_cols):
            cell = row.cells[idx]
            cell.text = ""  # buang paragraf kosong bawaan
            if idx >= len(cells_html):
                continue
            cell_tag = cells_html[idx]
            paragraph = cell.paragraphs[0]
            blocks = [c for c in cell_tag.find_all("p", recursive=False)]
            if blocks:
                for i, block in enumerate(blocks):
                    target = paragraph if i == 0 else cell.add_paragraph()
                    _add_runs(target, block, _Fmt(bold=cell_tag.name == "th"))
                    _apply_alignment(target, block)
            else:
                _add_runs(paragraph, cell_tag, _Fmt(bold=cell_tag.name == "th"))


def _render_block(document, tag: Tag) -> None:
    name = tag.name.lower()

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        heading = document.add_heading("", level=min(level, 4))
        _add_runs(heading, tag, _Fmt())
        _apply_alignment(heading, tag)
    elif name == "p":
        paragraph = document.add_paragraph()
        _add_runs(paragraph, tag, _Fmt())
        _apply_alignment(paragraph, tag)
    elif name == "ul":
        _render_list(document, tag, ordered=False)
    elif name == "ol":
        _render_list(document, tag, ordered=True)
    elif name == "blockquote":
        for child in tag.find_all(["p"], recursive=False) or [tag]:
            try:
                paragraph = document.add_paragraph(style="Quote")
            except KeyError:
                paragraph = document.add_paragraph()
                paragraph.paragraph_format.left_indent = Pt(24)
            _add_runs(paragraph, child, _Fmt(italic=True))
    elif name == "pre":
        paragraph = document.add_paragraph()
        run = paragraph.add_run(tag.get_text())
        run.font.name = "Courier New"
        run.font.size = Pt(9.5)
    elif name == "table":
        _render_table(document, tag)
    elif name == "hr":
        _add_hr(document)
    elif name == "img":
        paragraph = document.add_paragraph()
        paragraph.add_run(f"[gambar: {tag.get('alt') or tag.get('src') or ''}]").italic = True
    elif name in ("div", "section", "article", "main", "body"):
        _render_children(document, tag)
    else:
        text = tag.get_text(strip=True)
        if text:
            paragraph = document.add_paragraph()
            _add_runs(paragraph, tag, _Fmt())


def _render_children(document, node) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            if str(child).strip():
                document.add_paragraph(re.sub(r"\s+", " ", str(child)).strip())
        elif isinstance(child, Tag):
            _render_block(document, child)


def html_to_docx(html: str, output_path: str, title: str | None = None) -> str:
    """Render ``html`` menjadi berkas .docx di ``output_path``."""
    document = Document()
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)

    soup = BeautifulSoup(html or "", "html.parser")
    root = soup.body or soup

    # Judul catatan hanya ditulis kalau isi dokumen belum mengawali dengan h1 yang sama,
    # supaya tidak muncul judul dobel di berkas Word.
    if title:
        first_h1 = root.find("h1")
        duplicate = bool(
            first_h1 and first_h1.get_text(strip=True).casefold() == title.strip().casefold()
        )
        if not duplicate:
            heading = document.add_heading(title, level=0)
            heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    _render_children(document, root)

    document.save(output_path)
    return output_path
