"""Render HTML → PDF memakai Chromium (Playwright).

Mesin cetak utama untuk laporan. Dipilih menggantikan xhtml2pdf karena
xhtml2pdf tidak mengenal flexbox, tidak dapat menghitung lebar kolom tabel
secara otomatis, dan gagal pada gambar berukuran besar — tiga hal yang justru
paling sering muncul pada laporan hasil impor. Chromium merender persis seperti
yang tampil pada pratinjau layar, sehingga pratinjau dan berkas PDF sama.

Ukuran halaman mengikuti template resmi kampus: A4 dengan margin atas 4 cm,
bawah 3 cm, kiri 4 cm, kanan 3 cm.
"""

import os
import re
from urllib.parse import urlparse

from app.services.pdf_exporter import resolve_uri

# Margin template Tugas Akhir ULBI.
MARGIN = {"top": "4cm", "bottom": "3cm", "left": "4cm", "right": "3cm"}

# Nomor halaman di tengah bawah. Chromium tidak mendukung `@bottom-center`
# milik CSS Paged Media, jadi nomor halaman disuntik lewat template footer.
_FOOTER = (
    "<div style=\"width:100%;font-family:'Times New Roman',serif;font-size:10pt;"
    'text-align:center;margin:0 3cm;color:#000;">'
    '<span class="pageNumber"></span></div>'
)
_HEADER = '<div style="display:none"></div>'

_IMG = re.compile(r"<img\b[^>]*>", re.I)
_SRC = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.I)


def _ke_file_url(html: str) -> str:
    """Ubah src gambar jadi URL file:// lokal; buang yang tak bisa dipetakan.

    Gambar hasil impor ditulis sebagai URL http ke peladen aplikasi sendiri.
    Membiarkan Chromium mengunduhnya berarti render PDF bergantung pada peladen
    yang sedang melayani permintaan ini — lambat dan rapuh. Berkasnya ada di
    disk, jadi dibaca langsung. Sumber yang tidak ada dibuang supaya tidak
    menyisakan ikon gambar rusak di laporan.
    """

    def repl(match: re.Match) -> str:
        tag = match.group(0)
        src = _SRC.search(tag)
        if not src:
            return tag
        asal = src.group(1)
        if asal.startswith(("data:", "file://")):
            return tag
        lokal = resolve_uri(asal)
        if urlparse(lokal).scheme in ("http", "https") or not os.path.exists(lokal):
            return ""
        url = "file:///" + os.path.abspath(lokal).replace("\\", "/")
        return tag.replace(asal, url)

    return _IMG.sub(repl, html)


def html_to_pdf(html: str, output_path: str) -> str:
    """Render HTML utuh → PDF A4 bermargin template kampus."""
    import tempfile

    from playwright.sync_api import sync_playwright

    html = _ke_file_url(html)
    # HTML ditulis ke berkas lalu dibuka lewat file://, bukan `set_content`.
    # Halaman hasil `set_content` ber-origin about:blank dan Chromium menolak
    # memuat sumber file:// dari sana, sehingga semua gambar jadi ikon rusak.
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".html", encoding="utf-8", delete=False
    )
    try:
        tmp.write(html)
        tmp.close()
        url = "file:///" + os.path.abspath(tmp.name).replace("\\", "/")
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--allow-file-access-from-files"])
            try:
                page = browser.new_page()
                page.goto(url, wait_until="load", timeout=120_000)
                # Gambar bisa selesai setelah `load`; ditunggu eksplisit agar
                # tidak ada gambar kosong di PDF.
                page.wait_for_function(
                    "Array.from(document.images).every(i => i.complete)",
                    timeout=120_000,
                )
                page.pdf(
                    path=output_path,
                    format="A4",
                    margin=MARGIN,
                    print_background=True,
                    display_header_footer=True,
                    header_template=_HEADER,
                    footer_template=_FOOTER,
                )
            finally:
                browser.close()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return output_path


def tersedia() -> bool:
    """True bila Playwright dan berkas browsernya benar-benar ada di disk.

    `executable_path` tetap mengembalikan jalur meski browsernya belum diunduh
    (atau versinya tidak cocok dengan versi Playwright yang terpasang), jadi
    keberadaan berkasnya diperiksa langsung.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:  # noqa: BLE001 — browser belum diunduh
        return False
