"""Lapis 1 (PRD v2.8 §2) — Document AST: struktur dokumen internal (JSON).

Satu-satunya sumber kebenaran struktur dokumen. Import membangun AST dari hasil
ekstraksi (markdown + penanda tata letak), preview merender langsung dari AST,
dan export akhir menurunkannya ke format keluaran. Editor tetap memakai markdown
(AST06): AST dikonversi balik dengan `ast_to_markdown`.

Node menyimpan metadata yang selama ini hilang di jalur markdown mentah:
- `cover_page`: halaman sampul, dirender ter-center penuh di container sendiri.
- `align`: "center"/"left" — penanda `<center>` dari ekstraktor.
- `image.width_ratio`: fraksi lebar teks asli di sumber (dari petunjuk ?w=),
  lebar tampil proporsional dengan cap maksimum, bukan hardcode 0.8.
- `heading.auto_num`: heading yang mendapat nomor otomatis (CSS counter) vs
  yang sudah membawa nomor sendiri dari sumber ("1.1 Latar Belakang", "Bab 1").
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

_ALIGN = Literal["left", "center", "right"]


class AstImageInfo(BaseModel):
    src: str = ""
    width_px: int | None = None
    height_px: int | None = None
    width_ratio: float | None = None
    caption: str | None = None


class AstTableInfo(BaseModel):
    rows: list[list[str]] = Field(default_factory=list)
    has_merged_cells: bool = False
    header: bool = True


class AstNode(BaseModel):
    type: str = "paragraph"  # cover_page|heading|paragraph|image|table|list|code|blockquote|citation_marker|page_break
    level: int | None = None  # khusus heading (1-6)
    text: str = ""
    align: _ALIGN = "left"
    auto_num: bool = False  # heading: ikut penomoran otomatis (CSS counter)
    children: list["AstNode"] = Field(default_factory=list)
    image: AstImageInfo | None = None
    table: AstTableInfo | None = None
    source_order: int = 0


class DocumentAst(BaseModel):
    title: str = ""
    nodes: list[AstNode] = Field(default_factory=list)
    import_meta: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str) -> "DocumentAst":
        return cls.model_validate_json(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Parsing markdown (keluaran ekstraktor PDF/DOCX + editor) → AST
# ─────────────────────────────────────────────────────────────────────────────

_PENANDA_CENTER_OPEN = re.compile(r"<center\s*/?>", re.I)
_PENANDA_CENTER_CLOSE = re.compile(r"</center\s*>", re.I)
_NEWPAGE = re.compile(r"<newpage\s*/?>", re.I)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_IMAGE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_BLOCKQUOTE = re.compile(r"^\s*>\s?(.*)$")
_SITASI = re.compile(r"^\[\d+\](?:\s*\[\d+\])*")
_ANGKA_BAWAAN = re.compile(r"^\d+(?:\.\d+)+\s+")
_BAB = re.compile(r"^(?:bab|chapter)\s+[ivxlcdm\d]+\b", re.I)
# Bagian muka yang tidak pernah bernomor (sama dengan typeset._FRONT_MATTER).
_FRONT_MATTER = (
    "daftar isi", "daftar tabel", "daftar gambar", "daftar notasi",
    "daftar lampiran", "daftar singkatan", "daftar pustaka", "kata pengantar",
    "abstrak", "abstract", "lembar pengesahan", "surat pernyataan",
    "halaman persembahan", "pedoman penggunaan", "motto", "riwayat hidup",
    "ucapan terima kasih",
)
_CALON_COVER = re.compile(
    r"\b(LAPORAN|TUGAS AKHIR|SKRIPSI|TESIS|DISERTASI|PROPOSAL|PROYEK AKHIR)\b",
    re.I,
)


def _garis_center(baris: str) -> bool:
    return bool(_PENANDA_CENTER_OPEN.search(baris))


def _gambar_width_ratio(url: str) -> tuple[str, float | None]:
    """Petunjuk lebar `?w=0.32` dari ekstraktor → (url bersih, rasio)."""
    m = re.search(r"\?w=(\d+(?:\.\d+)?)", url)
    if not m:
        return url, None
    ratio = min(0.98, max(0.05, float(m.group(1))))
    return re.sub(r"\?w=[^&]*", "", url).rstrip("?"), ratio


def _mark_auto_num(nodes: list[AstNode]) -> None:
    """Tandai heading yang boleh dinomor otomatis.

    Sama dengan kaidah typeset._number_headings, tapi diterapkan di lapisan
    struktur: halaman depan/sampul tanpa nomor, judul yang sudah membawa
    nomornya sendiri tidak dihitung, dan judul bab yang terbelah ("Bab 1" lalu
    "PENDAHULUAN") disatukan — bagian keduanya tidak dinomor sebagai bab baru.
    """

    def teks(node: AstNode) -> str:
        return re.sub(r"[*_`]", "", node.text).strip()

    # Laporan yang memakai penanda "Bab N": bagian sebelum bab pertama (sampul,
    # pengesahan, daftar ...) dibiarkan tanpa nomor. Tanpa penanda tersebut,
    # penomoran langsung dimulai dari heading bagian pertama.
    indeks_bab = next(
        (i for i, n in enumerate(nodes)
         if n.type == "heading" and _BAB.match(teks(n))),
        None,
    )
    ada_bab = indeks_bab is not None

    lanjutan_bab = False
    for i, node in enumerate(nodes):
        if node.type == "cover_page":
            for child in node.children:
                if child.type == "heading":
                    child.auto_num = False
            continue
        if node.type != "heading" or not node.level:
            continue
        if ada_bab and i < indeks_bab:
            node.auto_num = False
            continue
        if _BAB.match(teks(node)):
            node.auto_num = False
            lanjutan_bab = True
            continue
        if lanjutan_bab:
            node.auto_num = False
            lanjutan_bab = False
            continue
        bawah = teks(node).lower()
        if any(bawah.startswith(fm) for fm in _FRONT_MATTER):
            node.auto_num = False
            continue
        if _ANGKA_BAWAAN.match(teks(node)):
            node.auto_num = False
            continue
        node.auto_num = True


def markdown_to_ast(markdown: str, *, title: str = "", meta: dict[str, Any] | None = None) -> DocumentAst:
    """Markdown hasil impor/editor → AST (AST06: representasi kanonik struktur)."""
    nodes: list[AstNode] = []
    stack_center: list[AstNode] = []  # pembungkus center bersarang (cover dll.)
    tabel_buffer: list[str] = []
    list_buffer: list[str] = []
    kode_buffer: list[str] = []

    def timpa_align(node: AstNode) -> AstNode:
        if stack_center:
            node.align = "center"
        return node

    def flush_tabel() -> None:
        nonlocal tabel_buffer
        if not tabel_buffer:
            return
        rows: list[list[str]] = []
        for baris in tabel_buffer:
            sel = [s.strip() for s in baris.strip().strip("|").split("|")]
            rows.append(sel)
        if len(rows) >= 2 and all(re.match(r"^:?-{2,}:?$", s) for s in rows[1]):
            rows.pop(1)  # baris pemisah pipa markdown
        tabel_buffer = []
        if rows:
            nodes.append(timpa_align(AstNode(
                type="table",
                table=AstTableInfo(rows=rows, has_merged_cells=False),
            )))

    def flush_list() -> None:
        nonlocal list_buffer
        if not list_buffer:
            return
        anak = [AstNode(type="paragraph", text=t) for t in list_buffer]
        list_buffer = []
        nodes.append(timpa_align(AstNode(type="list", children=anak)))

    def flush_kode() -> None:
        nonlocal kode_buffer
        if not kode_buffer:
            return
        isi = "\n".join(kode_buffer).strip("\n")
        kode_buffer = []
        nodes.append(timpa_align(AstNode(type="code", text=isi)))

    for nomor, baris in enumerate((markdown or "").splitlines()):
        if _garis_center(baris):
            stack_center.append(AstNode(type="paragraph", align="center"))
            continue
        if _PENANDA_CENTER_CLOSE.search(baris):
            flush_tabel()
            flush_list()
            flush_kode()
            if stack_center:
                stack_center.pop()
            continue
        if _NEWPAGE.search(baris):
            flush_tabel()
            flush_list()
            flush_kode()
            nodes.append(timpa_align(AstNode(type="page_break")))
            continue

        if baris.strip().startswith("```"):
            flush_tabel()
            flush_list()
            if not kode_buffer:
                kode_buffer.append("")  # pembuka fence
                continue
            flush_kode()
            continue
        if kode_buffer:
            kode_buffer.append(baris)
            continue

        if _TABLE_ROW.match(baris):
            flush_list()
            flush_kode()
            tabel_buffer.append(baris)
            continue
        if tabel_buffer and baris.strip() == "":
            flush_tabel()
            continue
        if tabel_buffer:
            tabel_buffer.append(baris)  # sel melompat baris
            continue

        m = _HEADING.match(baris)
        if m:
            flush_tabel()
            flush_list()
            flush_kode()
            level = len(m.group(1))
            nodes.append(timpa_align(AstNode(
                type="heading", level=level, text=m.group(2).strip(),
            )))
            continue

        m = _IMAGE.match(baris)
        if m:
            flush_tabel()
            flush_list()
            flush_kode()
            url, ratio = _gambar_width_ratio(m.group(2))
            nodes.append(timpa_align(AstNode(
                type="image",
                text=m.group(1).strip() or "Gambar",
                image=AstImageInfo(src=url, width_ratio=ratio),
            )))
            continue

        if _LIST_ITEM.match(baris):
            flush_tabel()
            flush_kode()
            list_buffer.append(_LIST_ITEM.match(baris).group(1).strip())
            continue
        if list_buffer and not baris.strip():
            flush_list()
            continue

        if _BLOCKQUOTE.match(baris):
            flush_tabel()
            flush_list()
            flush_kode()
            nodes.append(timpa_align(AstNode(
                type="blockquote", text=_BLOCKQUOTE.match(baris).group(1).strip(),
            )))
            continue

        if not baris.strip():
            flush_tabel()
            flush_list()
            flush_kode()
            continue

        # Baris sitasi daftar pustaka ("[1] Author, Judul...") berdiri sendiri.
        if _SITASI.match(baris.strip()):
            flush_tabel()
            flush_list()
            flush_kode()
            nodes.append(timpa_align(AstNode(type="citation_marker", text=baris.strip())))
            continue

        flush_tabel()
        flush_list()
        nodes.append(timpa_align(AstNode(type="paragraph", text=baris.rstrip())))

    flush_tabel()
    flush_list()
    flush_kode()
    if kode_buffer:
        nodes.append(AstNode(type="code", text="\n".join(kode_buffer)))

    # Sampul (AST02): blok center pertama yang memuat beberapa node (logo +
    # judul berbaris-baris) adalah halaman sampul, dirender ter-center penuh.
    # Blok `<center>` lain (judul bab di tengah) hanya 1-2 baris.
    cover: list[AstNode] = []
    if nodes and nodes[0].align == "center":
        run: list[AstNode] = []
        for n in nodes:
            if n.align == "center":
                run.append(n)
            else:
                break
        berisi_banyak = sum(1 for n in run if n.type != "page_break") >= 3
        ada_tanda = any(_CALON_COVER.search(n.text) for n in run[:3])
        if berisi_banyak or ada_tanda:
            cover = run
            nodes = nodes[len(run):]
    if cover:
        nodes.insert(0, AstNode(type="cover_page", align="center", children=cover))

    # Judul dokumen hanya dihitung sekali. Heading di luar sampul yang teksnya
    # sama dengan judul (mis. halaman pengesahan yang memuat ulang judul TA)
    # diturunkan jadi paragraf — mencegah judul ganda di editor dan struktur,
    # tanpa menghapus isi (paragraf tetap ikut tersimpan & ter-export).
    # Judul diturunkan dari heading pertama sampul (bukan `title`, karena
    # `title` di sini sering nama berkas, bukan judul sebenarnya).
    judul = title or ""
    if cover:
        for n in cover:
            if n.type == "heading" and n.text:
                judul = n.text
                break
    kunci = re.sub(r"[*_`\s]+", "", judul).lower()
    if kunci:
        for i, n in enumerate(nodes):
            if n.type == "heading" and re.sub(r"[*_`\s]+", "", n.text).lower() == kunci:
                nodes[i] = AstNode(type="paragraph", text=n.text, align=n.align)

    for i, node in enumerate(nodes):
        node.source_order = i
    _mark_auto_num(nodes)
    return DocumentAst(title=title, nodes=nodes, import_meta=meta or {})


# ─────────────────────────────────────────────────────────────────────────────
# AST → Markdown (editor, AST06) & AST → HTML (preview, PRD §3)
# ─────────────────────────────────────────────────────────────────────────────

def ast_to_markdown(ast: DocumentAst) -> str:
    """AST → markdown untuk editor (tidak ada penomoran otomatis, hanya struktur)."""
    keluar: list[str] = []

    def tulis(node: AstNode, dalam_center: bool = False) -> None:
        is_center = node.align == "center" and node.type != "cover_page"
        if is_center and not dalam_center:
            keluar.append("<center>")
        if node.type == "cover_page":
            keluar.append("<center>")
            for child in node.children:
                tulis(child, dalam_center=True)
            keluar.append("</center>")
        elif node.type == "heading":
            keluar.append(f"{'#' * (node.level or 1)} {node.text}")
        elif node.type == "paragraph":
            keluar.append(node.text)
        elif node.type == "image":
            src = node.image.src if node.image else node.text
            if node.image and node.image.width_ratio:
                src = f"{src}?w={node.image.width_ratio:.2f}"
            keluar.append(f"![{node.text or 'Gambar'}]({src})")
        elif node.type == "table" and node.table:
            rows = node.table.rows
            if rows:
                keluar.append("| " + " | ".join(rows[0]) + " |")
                keluar.append("| " + " | ".join(["---"] * len(rows[0])) + " |")
                keluar.extend("| " + " | ".join(r) + " |" for r in rows[1:])
        elif node.type == "list":
            for anak in node.children:
                keluar.append(f"- {anak.text}")
        elif node.type == "code":
            keluar.append("```")
            keluar.append(node.text)
            keluar.append("```")
        elif node.type == "blockquote":
            keluar.append(f"> {node.text}")
        elif node.type == "citation_marker":
            keluar.append(node.text)
        elif node.type == "page_break":
            keluar.append("<newpage>")
        if is_center and not dalam_center:
            keluar.append("</center>")

    for node in ast.nodes:
        tulis(node)
    return "\n\n".join(filter(None, keluar))


_AST_CSS = """
@page { size: A4; margin: 2.2cm 2cm 2cm 2cm; }
body { font-family: 'Times New Roman', Georgia, serif; font-size: 12pt;
       line-height: 1.6; color: #111; text-align: justify; }
h1, h2, h3, h4 { font-family: 'Times New Roman', serif; color: #000;
                 text-align: left; }
h1 { font-size: 16pt; margin: 0.9em 0 0.4em; counter-reset: sub; }
h1.auto { counter-increment: bab; }
h1.auto::before { content: counter(bab) ". "; font-weight: bold; }
h2 { font-size: 14pt; margin: 0.8em 0 0.3em; }
h2.auto { counter-increment: sub; }
h2.auto::before { content: counter(bab) "." counter(sub) " "; }
h3 { font-size: 12.5pt; margin: 0.6em 0 0.2em; }
table { border-collapse: collapse; width: 100%; margin: 0.6em auto; }
td, th { border: 1px solid #888; padding: 4px 8px; font-size: 11pt; }
th { background: #f2f2f2; }
img { max-width: 100%; display: block; margin: 0.5em auto; }
figure { margin: 0.8em 0; }
blockquote { border-left: 3px solid #bbb; margin: 0.6em 0; padding-left: 1em;
             color: #444; }
pre, code { font-family: 'Courier New', monospace; font-size: 10pt; }
pre { background: #f7f7f7; padding: 8px; border: 1px solid #ddd; }
/* Sampul (AST02): container ter-center penuh di halaman sendiri. */
.cover-page { text-align: center; page-break-after: always; }
.cover-page p, .cover-page h1, .cover-page h2, .cover-page h3 { text-align: center; }
.cover-page img { margin-left: auto; margin-right: auto; }
/* Halaman baru yang ditandai ekstraktor (bagian muka, awal bab). */
.page-break { page-break-before: always; }
/* Perataan tengah yang ditandai ekstraktor (judul muka di tengah). */
.center-block { text-align: center; }
"""


def ast_to_html(ast: DocumentAst) -> str:
    """Render AST → HTML pratinjau (PRD §3): toleran, tanpa compile apa pun.

    Penomoran heading memakai CSS counter (bukan teks hasil generate), gambar
    proporsional dengan lebar asli di sumber (cap 90% lebar konten), dan node
    yang tidak lengkap tetap dirender dengan nilai bawaan yang masuk akal.
    """
    keluar: list[str] = ["<html><head><meta charset='utf-8'>",
                         f"<style>{_AST_CSS}</style></head><body>"]

    def kelas(node: AstNode) -> str:
        if node.type == "heading":
            return " auto" if node.auto_num else ""
        return ""

    def tulis(node: AstNode) -> None:
        if node.type == "cover_page":
            keluar.append("<section class='cover-page'>")
            for child in node.children:
                tulis(child)
            keluar.append("</section>")
        elif node.type == "heading":
            tag = f"h{min(node.level or 1, 4)}"
            teks = _inline_md(node.text)
            if node.align == "center":
                keluar.append(f"<{tag} class='center-block{kelas(node)}'>{teks}</{tag}>")
            else:
                keluar.append(f"<{tag} class='{kelas(node).strip() or 'section'}'>{teks}</{tag}>")
        elif node.type == "paragraph":
            teks = _inline_md(node.text)
            if node.align == "center":
                keluar.append(f"<p class='center-block'>{teks}</p>")
            else:
                keluar.append(f"<p>{teks}</p>")
        elif node.type == "image":
            img = node.image or AstImageInfo(src=node.text)
            lebar = ""
            if img.width_ratio:
                lebar = f" style='width: min(90%, {img.width_ratio * 100:.1f}%)'"
            keluar.append(f"<figure><img src='{_html_escape(img.src)}'{lebar}>"
                          f"<figcaption class='caption'>{_inline_md(node.text or 'Gambar')}</figcaption></figure>")
        elif node.type == "table" and node.table:
            rows = node.table.rows
            if not rows:
                return
            keluar.append("<table><tbody>")
            for i, row in enumerate(rows):
                tag = "th" if (node.table.header and i == 0) else "td"
                keluar.append("<tr>" + "".join(
                    f"<{tag}>{_inline_md(c) if c else '&nbsp;'}</{tag}>" for c in row
                ) + "</tr>")
            keluar.append("</tbody></table>")
        elif node.type == "list":
            keluar.append("<ul>")
            for anak in node.children:
                keluar.append(f"<li>{_inline_md(anak.text)}</li>")
            keluar.append("</ul>")
        elif node.type == "code":
            keluar.append(f"<pre>{_html_escape(node.text)}</pre>")
        elif node.type == "blockquote":
            keluar.append(f"<blockquote>{_inline_md(node.text)}</blockquote>")
        elif node.type == "citation_marker":
            keluar.append(f"<p class='citation'>{_inline_md(node.text)}</p>")
        elif node.type == "page_break":
            keluar.append("<div class='page-break'></div>")

    for node in ast.nodes:
        tulis(node)
    keluar.append("</body></html>")
    return "\n".join(keluar)


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


# Inline markdown → HTML: **tebal**, *miring*/_miring_, `kode`, ~~coret~~,
# [teks](url). Jalur AST (preview mode Sumber & PDF) tidak lewat markdown2, jadi
# tanpa ini penanda inline tercetak harfiah ("banyak bintang"). Blok kode (<pre>)
# sengaja TIDAK diproses supaya isinya tetap apa adanya.
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_MD_STRIKE = re.compile(r"~~(.+?)~~")
_MD_ITALIC_STAR = re.compile(r"(?<!\*)\*(?!\s)([^*\n]+?)\*(?!\*)")
_MD_ITALIC_US = re.compile(r"(?<!\w)_(?!\s)([^_\n]+?)_(?!\w)")


def _inline_md(text: str) -> str:
    """Escape HTML lalu ubah penanda inline markdown jadi tag yang benar."""
    if not text:
        return _html_escape(text)
    simpanan: list[str] = []

    def _stash(fragmen: str) -> str:
        simpanan.append(fragmen)
        return f"\x00{len(simpanan) - 1}\x00"

    # 1) Amankan span kode dulu (isinya tak boleh ditafsir sebagai emfasis).
    hasil = _MD_CODE.sub(
        lambda m: _stash(f"<code>{_html_escape(m.group(1))}</code>"), text
    )
    # 2) Escape sisa teks; placeholder \x00..\x00 tak punya &<>" jadi selamat.
    hasil = _html_escape(hasil)
    # 3) Tautan sebelum emfasis, lalu diamankan agar URL ber-_ tak ikut miring.
    hasil = _MD_LINK.sub(
        lambda m: _stash(f'<a href="{m.group(2)}">{m.group(1)}</a>'), hasil
    )
    # 4) Tebal dulu (agar ** tak tertukar dengan *), lalu coret, lalu miring.
    hasil = _MD_BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", hasil)
    hasil = _MD_STRIKE.sub(lambda m: f"<del>{m.group(1)}</del>", hasil)
    hasil = _MD_ITALIC_STAR.sub(lambda m: f"<em>{m.group(1)}</em>", hasil)
    hasil = _MD_ITALIC_US.sub(lambda m: f"<em>{m.group(1)}</em>", hasil)
    # 5) Kembalikan span kode & tautan.
    for i, fragmen in enumerate(simpanan):
        hasil = hasil.replace(f"\x00{i}\x00", fragmen)
    return hasil
