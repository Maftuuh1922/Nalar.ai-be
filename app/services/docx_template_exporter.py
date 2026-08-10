"""Ekspor markdown → DOCX memakai template resmi Tugas Akhir kampus.

Berbeda dari `docx_exporter` yang membangun dokumen kosong lalu menata sendiri
fontnya, modul ini membuka berkas template kampus dan menulis isi laporan ke
dalamnya. Dengan begitu margin (atas 4, bawah 3, kiri 4, kanan 3 cm), font,
spasi 1,5, serta gaya heading dan keterangan gambar mengikuti aturan resmi
tanpa perlu ditiru ulang — dan berkas hasilnya tetap bisa disunting di Word.
"""

import io
import os
import re

from docx import Document
from docx.enum.text import WD_BREAK, WD_PARAGRAPH_ALIGNMENT
from docx.shared import Cm, Pt

from app.services.pdf_exporter import resolve_uri

# Template dicari relatif terhadap akar proyek (satu tingkat di atas backend).
_NAMA_TEMPLATE = "02 Template Laporan Tugas Akhir.docx"

# Lebar area ketik = lebar A4 dikurangi margin kiri+kanan template (4 + 3 cm).
_LEBAR_ISI_CM = 14.0

# Batas lebar gambar yang ditanam ke DOCX (dalam piksel). PDF TA hasil impor
# biasanya menyimpan citra 3200x2000; menanamnya utuh membuat berkas DOCX
# membengkak hingga beberapa MB sehingga konversi pratinjau ONLYOFFICE
# memakan waktu sangat lama. Dipakai kapasitas ~1/2 ukuran asli.
_MAKS_LEBAR_PX = 1200
_MAKS_TINGGI_PX = 900

_GAMBAR = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)$")
_BARIS_TABEL = re.compile(r"^\|.*\|$")
_PEMISAH_TABEL = re.compile(r"^\|?[\s:|-]*-{2,}[\s:|-]*\|?$")
_CENTER_BUKA = re.compile(r"^<center\s*/?>", re.I)
_CENTER_TUTUP = re.compile(r"^</center\s*>", re.I)
_NEWPAGE = re.compile(r"^<newpage\s*/?>", re.I)


def cari_template() -> str | None:
    """Jalur template kampus, atau None bila tidak ditemukan."""
    dari_env = os.environ.get("NALAR_AI_TEMPLATE_DOCX")
    if dari_env and os.path.exists(dari_env):
        return dari_env
    dasar = os.path.abspath(os.getcwd())
    for _ in range(4):
        kandidat = os.path.join(dasar, _NAMA_TEMPLATE)
        if os.path.exists(kandidat):
            return kandidat
        induk = os.path.dirname(dasar)
        if induk == dasar:
            break
        dasar = induk
    return None


def _kosongkan_isi(document) -> None:
    """Buang isi template, pertahankan gaya dan pengaturan halaman.

    Gaya, margin, header/footer tersimpan di bagian lain berkas, bukan di badan
    dokumen. `sectPr` (pengaturan seksi) ada di akhir badan dan harus disisakan
    karena di situlah ukuran kertas dan marginnya tercatat.
    """
    body = document.element.body
    for anak in list(body.iterchildren()):
        if not anak.tag.endswith("}sectPr"):
            body.remove(anak)


def _gaya_ada(document, nama: str) -> bool:
    try:
        document.styles[nama]
        return True
    except KeyError:
        return False


def _paragraf_daftar(document, gaya: str, penanda: str):
    """Paragraf daftar bergaya `gaya`.

    Template kampus tidak menyertakan gaya "List Bullet"/"List Number", jadi
    bila gaya itu tidak ada paragrafnya dibuat manual: diberi indentasi dan
    penanda teks supaya daftarnya tetap terlihat sebagai daftar.
    """
    if _gaya_ada(document, gaya):
        return document.add_paragraph(style=gaya), ""
    p = document.add_paragraph()
    p.paragraph_format.left_indent = Cm(1.0)
    return p, penanda


def _sel_baris(line: str) -> list[str]:
    s = line.strip().strip("|")
    return [c.replace("<br>", " ").strip() for c in s.split("|")]


def _tulis_tabel(document, baris: list[str]) -> None:
    isi = [_sel_baris(b) for b in baris if not _PEMISAH_TABEL.match(b.strip())]
    isi = [r for r in isi if any(r)]
    if not isi:
        return
    kolom = max(len(r) for r in isi)
    tabel = document.add_table(rows=0, cols=kolom)
    if _gaya_ada(document, "Table Grid"):
        tabel.style = "Table Grid"
    for i, r in enumerate(isi):
        sel = tabel.add_row().cells
        for j in range(kolom):
            teks = re.sub(r"[*_`]", "", r[j]) if j < len(r) else ""
            sel[j].text = teks
            if i == 0:
                for p in sel[j].paragraphs:
                    for run in p.runs:
                        run.bold = True


def _tulis_gambar(document, src: str, alt: str) -> None:
    lokal = resolve_uri(src)
    if not os.path.exists(lokal):
        return
    try:
        from PIL import Image

        with Image.open(lokal) as im:
            im = im.convert("RGB")
            im.thumbnail((_MAKS_LEBAR_PX, _MAKS_TINGGI_PX), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80, optimize=True)
            buf.seek(0)
            document.add_picture(buf, width=Cm(_LEBAR_ISI_CM))
    except Exception:  # noqa: BLE001 - berkas rusak/format tak didukung Word
        return
    document.paragraphs[-1].alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    if alt and alt.lower() not in ("gambar", "alt", "image"):
        gaya = "Judul Gambar & Tabel" if _gaya_ada(document, "Judul Gambar & Tabel") else None
        p = document.add_paragraph(alt, style=gaya) if gaya else document.add_paragraph(alt)
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER


def markdown_to_docx_template(
    markdown_text: str,
    output_path: str,
    references: dict[int, str] | None = None,
    template_path: str | None = None,
) -> str:
    """Tulis markdown ke DOCX berformat template kampus.

    Bila template tidak ditemukan, dilempar ke `markdown_to_docx` biasa supaya
    ekspor tetap berjalan, hanya tanpa format resmi.
    """
    from app.services.docx_exporter import _split_inline, markdown_to_docx

    template = template_path or cari_template()
    if not template:
        return markdown_to_docx(markdown_text, output_path, references)

    references = references or {}
    document = Document(template)
    _kosongkan_isi(document)

    baris = (markdown_text or "").split("\n")
    i = 0
    dalam_center = False
    while i < len(baris):
        line = baris[i].strip()
        if not line:
            i += 1
            continue

        if _CENTER_BUKA.match(line):
            dalam_center = True
            i += 1
            continue
        if _CENTER_TUTUP.match(line):
            dalam_center = False
            i += 1
            continue
        if _NEWPAGE.match(line):
            p = document.add_paragraph()
            p.add_run().add_break(WD_BREAK.PAGE)
            i += 1
            continue

        if _BARIS_TABEL.match(line):
            blok = []
            while i < len(baris) and _BARIS_TABEL.match(baris[i].strip()):
                blok.append(baris[i])
                i += 1
            _tulis_tabel(document, blok)
            continue

        gbr = _GAMBAR.match(line)
        if gbr:
            _tulis_gambar(document, gbr.group("src"), gbr.group("alt"))
            i += 1
            continue

        # Blok kode ```...``` (dari \\begin{verbatim}) → paragraf monospace.
        # Tanpa ini, baris kode yang diawali angka jatuh ke daftar bernomor dan
        # listing lampiran tercerai-berai jadi fragmen.
        if line.startswith("```"):
            i += 1
            kode: list[str] = []
            while i < len(baris) and not baris[i].strip().startswith("```"):
                kode.append(baris[i])
                i += 1
            i += 1  # lewati penutup ```
            for baris_kode in kode:
                p = document.add_paragraph()
                run = p.add_run(baris_kode if baris_kode else "")
                run.font.name = "Consolas"
                run.font.size = Pt(9)
            continue

        judul = re.match(r"^(#{1,4})\s+(.*)$", line)
        if judul:
            tingkat = len(judul.group(1))
            # Heading dibuat kosong dulu, lalu isinya diparsing inline supaya
            # `# **Judul**` tidak tercetak dengan tanda bintang mentah.
            h = document.add_heading("", level=tingkat)
            _split_inline(h, judul.group(2), references)
            if tingkat == 1 or dalam_center:
                h.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        elif line.startswith(("- ", "* ")):
            p, penanda = _paragraf_daftar(document, "List Bullet", "• ")
            _split_inline(p, penanda + line[2:], references)
            if dalam_center:
                p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        elif re.match(r"^\d+\.\s", line):
            nomor = re.match(r"^(\d+\.)\s", line).group(1)
            p, penanda = _paragraf_daftar(document, "List Number", f"{nomor} ")
            _split_inline(p, penanda + re.sub(r"^\d+\.\s", "", line), references)
            if dalam_center:
                p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        else:
            p = document.add_paragraph()
            _split_inline(p, line, references)
            if dalam_center:
                p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        i += 1

    # Word (OOXML) requires that a document ends with a paragraph (<w:p>) before <w:sectPr>.
    # If the markdown ends with a table or something else, Word will consider the DOCX corrupted
    # and OnlyOffice will fail or become extremely slow. Always ensure a paragraph at the end.
    document.add_paragraph()

    document.save(output_path)
    return output_path
