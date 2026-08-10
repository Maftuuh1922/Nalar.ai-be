"""Export draf Co-Writer ke PDF (via markdown2 + xhtml2pdf).

Menjaga heading/numbering/tabel/daftar pustaka — bukan render teks polos.
"""

import os
import re
from io import BytesIO
from urllib.parse import unquote, urlparse

import markdown2
from xhtml2pdf import pisa

_CSS = """
@page { size: A4; margin: 2cm; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 11pt; line-height: 1.5; color: #111; }
h1 { font-size: 18pt; margin: 0.8em 0 0.3em; }
h2 { font-size: 15pt; margin: 0.7em 0 0.3em; }
h3 { font-size: 13pt; margin: 0.6em 0 0.2em; }
h4 { font-size: 11.5pt; margin: 0.5em 0 0.2em; }
table { border-collapse: collapse; width: 100%; margin: 0.5em 0; }
td, th { border: 1px solid #999; padding: 4px 8px; }
pre, code { font-family: 'Courier New', monospace; font-size: 9.5pt; background: #f4f4f4; }
blockquote { border-left: 3px solid #ccc; margin: 0.5em 0; padding-left: 1em; color: #555; }
"""


def resolve_uri(uri: str, rel: str | None = None) -> str:
    """Petakan URL gambar hasil impor ke berkas lokalnya.

    Gambar dokumen disajikan lewat route statis /uploads pada peladen yang sama.
    Bila xhtml2pdf mengambilnya lewat HTTP, permintaan itu menunggu peladen yang
    justru sedang sibuk merender PDF ini — saling menunggu dan ekspor tak pernah
    selesai. Karena berkasnya ada di disk, jalur lokal dipakai langsung.
    """
    parsed = urlparse(uri)
    if parsed.scheme in ("http", "https") and parsed.path.startswith("/uploads/"):
        lokal = os.path.join(os.getcwd(), unquote(parsed.path.lstrip("/")))
        if os.path.exists(lokal):
            return lokal
    return uri


# Sel tabel kosong membuat xhtml2pdf menghitung lebar kolomnya nol lalu gagal
# dengan "negative availWidth"; diisi spasi keras agar tetap punya lebar.
_SEL_KOSONG = re.compile(r"<(td|th)([^>]*)>\s*</\1>")

_IMG = re.compile(r"<img\b(?![^>]*\bwidth=)[^>]*?>", re.I)
_SRC = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.I)

# Area cetak A4 dikurangi margin 2 cm, dalam sentimeter.
_LEBAR_CETAK_CM = 17.0
_TINGGI_CETAK_CM = 25.0


def _ukuran_asli_cm(src: str) -> tuple[float, float] | None:
    """Ukuran alami gambar (lebar, tinggi) dalam cm pada 96 dpi, atau None."""
    jalur = resolve_uri(src)
    if urlparse(jalur).scheme in ("http", "https") or not os.path.exists(jalur):
        return None
    try:
        from PIL import Image

        with Image.open(jalur) as im:
            return (im.width / 96 * 2.54, im.height / 96 * 2.54)
    except Exception:  # noqa: BLE001 — berkas rusak/format tak dikenal
        return None


def _batasi_ukuran_gambar(html: str) -> str:
    """Perkecil <img> yang melebihi area cetak, menjaga rasio aslinya.

    Gambar hasil impor kerap berukuran ribuan piksel (mis. 3200x2000 ≈ 84x53 cm
    pada 96 dpi). Tanpa batas ukuran, reportlab menganggapnya terlalu besar untuk
    satu halaman dan gagal — lalu penyusunan pesan errornya sendiri ikut galat,
    sehingga penyebab aslinya tersamar. Lebar dan tinggi ditulis berpasangan
    karena xhtml2pdf tidak menurunkan sisi yang tidak disebut, sehingga gambar
    lebar yang hanya dibatasi lebarnya tetap menjulang melewati halaman.
    """

    def repl(match: re.Match) -> str:
        tag = match.group(0)
        src = _SRC.search(tag)
        if not src:
            return tag
        ukuran = _ukuran_asli_cm(src.group(1))
        if ukuran is None:
            # Sumber tak dikenali (mis. contoh `![alt](url)`). Dibuang karena
            # xhtml2pdf akan mencoba mengunduhnya dan bisa menggantung lama.
            if not urlparse(src.group(1)).scheme:
                return ""
            return tag
        lebar, tinggi = ukuran
        skala = min(_LEBAR_CETAK_CM / lebar, _TINGGI_CETAK_CM / tinggi, 1.0)
        if skala >= 1.0:
            return tag
        awal = tag[:-1].rstrip("/").rstrip()
        return f'{awal} width="{lebar * skala:.2f}cm" height="{tinggi * skala:.2f}cm" />'

    return _IMG.sub(repl, html)


_HANYA_GAMBAR = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")


def _pisahkan_baris_gambar(markdown_text: str) -> str:
    """Beri baris kosong di sekitar baris yang isinya hanya gambar.

    Beberapa gambar pada baris berurutan tanpa baris kosong dianggap markdown
    sebagai satu paragraf. Paragraf berisi banyak gambar bisa lebih tinggi dari
    satu halaman, dan reportlab tidak dapat memecah paragraf bergambar sehingga
    ekspor gagal. Dipisah agar tiap gambar berdiri sebagai blok sendiri.
    """
    keluar: list[str] = []
    for baris in markdown_text.splitlines():
        if _HANYA_GAMBAR.match(baris):
            if keluar and keluar[-1].strip():
                keluar.append("")
            keluar.append(baris.strip())
            keluar.append("")
        else:
            keluar.append(baris)
    return "\n".join(keluar)


def markdown_to_pdf(markdown_text: str, output_path: str) -> str:
    """Ubah markdown → PDF (A4, heading & tabel rapi)."""
    html_body = markdown2.markdown(
        _pisahkan_baris_gambar(markdown_text),
        extras=["fenced-code-blocks", "tables", "strike", "task_list"],
    )
    html_body = _SEL_KOSONG.sub(r"<\1\2>&nbsp;</\1>", html_body)
    html_body = _batasi_ukuran_gambar(html_body)
    html = (
        f"<html><head><meta charset='utf-8'><style>{_CSS}</style></head>"
        f"<body>{html_body}</body></html>"
    )
    buf = BytesIO()
    try:
        result = pisa.CreatePDF(html, dest=buf, link_callback=resolve_uri)
    except TypeError as exc:  # noqa: PERF203 — bug hulu, pesannya perlu diterjemahkan
        # xhtml2pdf gagal saat menyusun pesan error reportlab (getPlainText
        # menggabungkan list, bukan string), sehingga sebab aslinya tersamar.
        # Umumnya elemen terlalu tinggi untuk satu halaman.
        raise RuntimeError(
            "Gagal membuat PDF: ada elemen yang terlalu besar untuk satu "
            "halaman (biasanya gambar atau tabel berukuran sangat besar)."
        ) from exc
    if result.err:
        raise RuntimeError("Gagal membuat PDF (xhtml2pdf error).")
    with open(output_path, "wb") as fh:
        fh.write(buf.getvalue())
    return output_path
