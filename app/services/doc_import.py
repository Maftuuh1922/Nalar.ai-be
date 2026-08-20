"""Impor dokumen (PDF/DOCX) ke markdown Co-Writer dengan struktur & gambar
dipertahankan (PRD: pipeline import).

- PDF: ekstraksi PyMuPDF langsung — tingkat heading ditentukan dari ukuran
  font, baris badan teks disambung kembali jadi paragraf oleh `_sambung_paragraf`.
  pymupdf4llm sempat dipakai karena hasilnya lebih rapi, tapi terlalu lambat
  untuk dokumen besar: laporan 89 halaman butuh 270+ detik, sedangkan jalur ini
  16 detik. Selisihnya menentukan apakah fitur impor bisa dipakai atau tidak.
- DOCX: python-docx, ditelusuri mengikuti urutan elemen badan dokumen
  (paragraf dan tabel) supaya gambar melekat pada paragraf pemiliknya.
- Gambar disimpan ke uploads/{doc_id}/images/ dan disisipkan sebagai markdown
  `![](url)` pada posisi kemunculannya.

Batasan yang dikomunikasikan: layout kompleks (multi-kolom, text box bebas
posisi) mungkin perlu penyesuaian manual — bukan 100% identik. Tabel tidak
dikenali sebagai tabel markdown; isinya tetap muncul sebagai teks.
"""

from __future__ import annotations

import io
import logging
import os
import re
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Heading jika ukuran font >= ambang ini atau bold + besar (jalur cadangan).
_HEADING_FONT_MIN = 14.0

# Konversi cm → pt (1 cm = 28.3465 pt). Margin LaTeX kampus (kiri 4 + kanan 3 cm)
# dipakai untuk mengubah lebar tampilan gambar di PDF menjadi fraksi \\textwidth.
_CM_PT = 28.346456692913385

# PDF yang dibuat dengan mencetak source `.tex` (mis. via browser/editor)
# menampilkan perintah LaTeX sebagai teks; PyMuPDF membaca backslash dari font
# simbol Computer Modern sebagai `\{}`. Penanda ini dipakai untuk menolak
# berkas semacam itu lebih awal, supaya pengguna tidak mendapat draf penuh
# kode LaTeX mentah yang mustahil dibetulkan manual.
_SOURCE_LATEX_CMD = re.compile(r"\\\{\}")
_SOURCE_LATEX_ENV = re.compile(r"\b(?:begin|end)\s*\{", re.I)
# PDF hasil kompilasi tak pernah memuat backslash font simbol Computer Modern
# (dibaca PyMuPDF sebagai `\{}`) — penanda andal source .tex mentah. Ambang
# total dipakai untuk PDF yang semuanya source mentah; ambang fraksi halaman
# menangkap source mentah yang tersebar merata tanpa harus sampai ratusan.
_SOURCE_LATEX_TOTAL_AMBANG = 100
_SOURCE_LATEX_PER_HALAMAN = 5
_SOURCE_LATEX_FRAKSI = 0.4

_PESAN_SOURCE_LATEX = (
    "PDF ini berisi source LaTeX mentah (perintah LaTeX seperti "
    "\\begin{...} tampil sebagai teks), bukan hasil kompilasi. "
    "Gunakan PDF laporan yang sudah dikompilasi, atau impor berkas .tex langsung."
)


def _hitung_penanda_latex_mentah(teks: str) -> int:
    """Jumlah penanda source LaTeX mentah (backslash + begin/end tanpa kurung)."""
    return len(_SOURCE_LATEX_CMD.findall(teks)) + len(_SOURCE_LATEX_ENV.findall(teks))


def _deteksi_source_latex_pdf(doc) -> None:
    """Tolak PDF yang isinya source .tex mentah, berbasis distribusi per halaman.

    Lampiran kode sumber program kadang memuat string Python berisi perintah
    LaTeX (mis. `out.append(r"\\begin{figure}")`), sehingga sebaran penandanya
    hanya di halaman lampiran. Source .tex yang dicetak menjadi PDF menyebar di
    hampir seluruh halaman. Aturan: tolak bila total penanda >= 100, atau bila
    >= 40% halaman masing-masing memuat >= 5 penanda.
    """
    total = 0
    halaman_berat = 0
    n = len(doc)
    for page in doc:
        k = _hitung_penanda_latex_mentah(page.get_text())
        total += k
        if k >= _SOURCE_LATEX_PER_HALAMAN:
            halaman_berat += 1
    if total >= _SOURCE_LATEX_TOTAL_AMBANG or (
        n > 0 and halaman_berat / n >= _SOURCE_LATEX_FRAKSI
    ):
        raise ValueError(_PESAN_SOURCE_LATEX)


# Format gambar yang tidak bisa disematkan tectonic/pdflatex. WMF/EMF adalah
# metafile Windows yang lazim muncul di DOCX (rumus Equation Editor, bullet
# khusus); membiarkannya membuat seluruh ekspor PDF laporan gagal dengan
# "image inclusion failed", bukan cuma satu gambar yang hilang.
_FORMAT_TAK_DIDUKUNG = {".wmf", ".emf"}


def _ke_png_bila_perlu(name: str, data: bytes) -> tuple[str, bytes]:
    """Konversi WMF/EMF ke PNG; format lain diteruskan apa adanya.

    Konversi WMF bergantung pada GDI Windows lewat Pillow. Bila gagal, nama dan
    isi aslinya dikembalikan — gambar tetap tersimpan supaya tidak hilang dari
    draf, dan kegagalannya muncul di log kompilasi, bukan di sini.
    """
    akar, ext = os.path.splitext(name)
    if ext.lower() not in _FORMAT_TAK_DIDUKUNG:
        return name, data
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            im.load()
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="PNG")
        return f"{akar}.png", buf.getvalue()
    except Exception:  # noqa: BLE001 — Pillow tanpa dukungan WMF di non-Windows
        logger.warning("Gagal mengonversi %s ke PNG; disimpan apa adanya.", name)
        return name, data


def _save_bytes(images_dir: str, name: str, data: bytes) -> str:
    """Simpan gambar dan kembalikan nama berkas yang benar-benar tertulis."""
    name, data = _ke_png_bila_perlu(name, data)
    os.makedirs(images_dir, exist_ok=True)
    with open(os.path.join(images_dir, name), "wb") as fh:
        fh.write(data)
    return name


def _ekstrak_gambar(
    doc, images_dir: str, base_url: str
) -> tuple[dict[int, str], list[tuple[int, str]]]:
    """Simpan gambar PDF sekali per xref; return (peta xref→url, [(halaman, url)])."""
    peta: dict[int, str] = {}
    out: list[tuple[int, str]] = []
    for page_index in range(len(doc)):
        for img in doc[page_index].get_images(full=True):
            xref = img[0]
            url = peta.get(xref)
            if url is None:
                try:
                    pix = doc.extract_image(xref)
                except Exception:  # noqa: BLE001
                    continue
                ext = pix.get("ext", "png") or "png"
                name = f"img_{len(peta) + 1:03d}.{ext}"
                data = pix.get("image")
                if not data:
                    continue
                _save_bytes(images_dir, name, data)
                url = f"{base_url}/{name}"
                peta[xref] = url
            # Satu aset dapat dipakai berulang (logo/header setiap halaman).
            # Berkasnya cukup disimpan sekali, tetapi setiap kemunculannya wajib
            # tetap dicatat agar laporan impor tidak kehilangan gambar.
            out.append((page_index, url))
    return peta, out


def _ext_images_from_bytes(
    doc, images_dir: str, base_url: str, doc_id: str
) -> list[tuple[int, str]]:
    """Ekstrak gambar per halaman dari PyMuPDF. Return [(halaman, markdown_path)]."""
    _, out = _ekstrak_gambar(doc, images_dir, base_url)
    return out


def pdf_to_markdown(contents: bytes, images_dir: str, base_url: str, doc_id: str) -> str:
    """PDF → markdown + ekstrak gambar.

    Jalur utama: PDF dikonversi ke DOCX kerja dulu lalu diekstrak dengan
    markitdown (paragraf tersambung, tabel rapi, tanpa HTML mentah) —
    hasilnya jauh lebih rapi daripada PyMuPDF manual. Bila markitdown atau
    DOCX tidak tersedia, fallback ke ekstraksi PyMuPDF sederhana (20× lebih
    cepat daripada pymupdf4llm untuk dokumen besar, cukup untuk editor teks
    yang naskahnya bisa diperbaiki manual).
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=contents, filetype="pdf")
    try:
        # Cek dulu sebelum ekstraksi berat: PDF berisi source .tex mentah tidak
        # layak diimpor dan hanya membuang waktu puluhan detik.
        _deteksi_source_latex_pdf(doc)
        markdown = _pdf_to_markdown_sederhana(doc, images_dir, base_url)
    finally:
        doc.close()

    return _rapikan(markdown)


def pdf_docx_to_markdown(
    docx_path: str | Path | None,
    contents: bytes,
    images_dir: str,
    base_url: str,
    doc_id: str,
) -> str:
    """PDF (yang sudah jadi DOCX kerja) → markdown rapi via markitdown.

    markitdown membaca DOCX hasil pipeline pdf2docx/postprocess dan
    menghasilkan markdown berstruktur (paragraf tersambung, tabel markdown,
    heading) — menggantikan hasil kasar pandoc/PyMuPDF manual untuk mode
    sumber/editor. Bila markitdown tidak terpasang atau gagal, fallback ke
    ekstraksi PyMuPDF sederhana.
    """
    try:
        from markitdown import MarkItDown

        if docx_path is None or not Path(docx_path).exists():
            raise FileNotFoundError(docx_path)
        md = MarkItDown()
        res = md.convert(str(docx_path), file_extension=".docx")
        out = (res.text_content or "").strip()
        # markitdown menghasilkan tabel pipa untuk layout teks acak; bila
        # hasilnya didominasi tabel noise (>=20% baris), buang dan fallback.
        baris = out.splitlines()
        if baris:
            tabel = sum(1 for ln in baris if ln.lstrip().startswith("|"))
            if tabel / len(baris) >= 0.2:
                raise ValueError(f"markitdown: output didominasi tabel layout ({tabel}/{len(baris)})")
        if len(out) < 200:
            raise ValueError("markitdown: output terlalu pendek")
        return _rapikan(out)
    except Exception as exc:  # noqa: BLE001 — markitdown gagal bukan alasan impor gagal
        logger.warning("markitdown gagal (%s); fallback PyMuPDF sederhana.", exc)
        return pdf_to_markdown(contents, images_dir, base_url, doc_id)


# Penanda highlight bawaan dari pengekstrak HTML; markdown2 tidak mengenalnya
# dan HTML mentah itu bocor ke tampilan sebagai sorotan kuning yang tidak diminta.
_MARK = re.compile(r"</?mark>")
# Baris pemisah halaman "-----" jadi garis horizontal bertubi-tubi di pratinjau.
_PEMISAH_HALAMAN = re.compile(r"^-{3,}\s*$")
# Heading "BAB 1" / "Bab 1" — bab utama dokumen akademik, ditandai H1.
_BAB = re.compile(r"^(?:bab|chapter)\s+[ivxlcdm\d]+\b", re.I)
# Bagian muka yang berulang di PDF kampus dan selalu jadi bagian utama (H1).
_FRONT = re.compile(
    r"^(?:lembar|surat|halaman|kata|daftar|abstrak|abstract|pernyataan|"
    r"persembahan|pedoman|mott|glosarium|biodata|riwayat|lampiran)\b",
    re.I,
)
_NOMOR_2 = re.compile(r"^\d+\.\d+\s")
_NOMOR_3 = re.compile(r"^\d+\.\d+\.\d+\s")
# Logo LaTeX yang diekstrak sebagai "L<sup>A</sup> TEX" (A bisa berbungkus
# penanda markdown) — disatukan jadi kata "LaTeX".
_LOGO_LATEX = re.compile(r"L<sup>[^<]*</sup>\s*TEX", re.I)
# Nomor halaman footer ("vi", "12") yang terisolasi pada barisnya sendiri.
_HALAMAN_ONLY = re.compile(r"^(?:[ivxlcdm]{1,7}|\d{1,3})$", re.I)
# Listing kode lampiran: pengekstrak menyatukan baris bernomor jadi satu
# paragraf panjang. Dipulihkan jadi blok kode bertanda.
_KODE_MARK = re.compile(
    r"def\s+\w+|import\s+\w+|self\.|\"\"\"|return\s|lambda\s|print\(|"
    r"class\s+\w+|\bassert\b|\bfor\s+\w+\s+in\b|\bif\s+\w+\s*(?:==|!=|<|>|in)\b"
)
_KODE_PECAH = re.compile(r"\s+(?=\d{1,3}\s+\S)")
_ANGKA_SAJA = re.compile(r"^\d{1,3}$")
_NOMOR_AWAL = re.compile(r"^\d{1,3}\s")
# Baris kode berakhir di tanda yang menunggu nilai (ekor terpotong).
_AKHIR_SAMBUNG = re.compile(
    r"[=+\-*/(\[{},:]$|(?:return|import|print|raise|yield|lambda)\s*$", re.I
)


def _punya_lanjutan(pecah: list[str], i: int) -> bool:
    """Adakah baris bernomor setelah deret angka terisolasi (artefak)?"""
    j = i + 1
    while j < len(pecah) and _ANGKA_SAJA.match(pecah[j]):
        j += 1
    return j < len(pecah) and bool(_NOMOR_AWAL.match(pecah[j]))


def _pulihkan_baris_kode(pecah: list[str]) -> list[str]:
    """Rekonstruksi baris kode yang terpotong oleh pengekstrak PDF.

    - Ekor baris yang terpisah ("5 fig_count =" / "0") digabung kembali.
    - Nomor baris yang kehilangan isinya ("11" sebelum "13 def ...") dibuang.
    """
    hasil: list[str] = []
    for i, p in enumerate(pecah):
        if _ANGKA_SAJA.match(p):
            if hasil and _AKHIR_SAMBUNG.search(hasil[-1]):
                hasil[-1] = f"{hasil[-1]} {p}"
                continue
            if _punya_lanjutan(pecah, i):
                continue
        hasil.append(p)
    return hasil


# Penanda blok Markdown di awal baris: pagar heading, kutipan, tabel pipa,
# penanda daftar, dan daftar bernomor.
# Penanda DAFTAR (`-`, `*`, `1.`) sengaja TIDAK dilindungi: pada laporan akademik
# baris semacam itu memang daftar sungguhan (surat pernyataan, ucapan terima
# kasih, butir analisis), dan melindunginya membuat "\1." tercetak apa adanya di
# editor. Terukur pada laporan uji: 105 baris `\N.` dan 4 baris `\-` seluruhnya
# daftar asli, sedangkan `#` hanya muncul 3 kali dan ketiganya komentar kode
# Python di lampiran — satu-satunya bentuk yang benar-benar merusak bila dibiarkan
# (menjadi heading H1 lalu `\section*`, mengacaukan outline dan pemecahan bab).
_AWALAN_BLOK_MD = re.compile(r"^(\s*)(#{1,6}(?=\s|$)|>|\|)")


def _lolos_awalan_markdown(teks: str) -> str:
    """Lindungi penanda blok Markdown di awal paragraf yang bukan heading.

    Paragraf DOCX biasa dikirim apa adanya ke Pandoc, sehingga komentar kode
    Python di lampiran ("# Kumpulkan teks dari aliran") terbaca sebagai heading
    H1 dan berakhir jadi `\\section*` — laporan hasil impor mendapat "bab" palsu
    yang mengacaukan outline dan pemecahan per bab.
    """
    return _AWALAN_BLOK_MD.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", teks, count=1)


def _rapikan(markdown: str, *, preserve_content: bool = True) -> str:
    """Bersihkan markup ekstraktor tanpa membuang isi sumber saat impor."""
    baris = [_MARK.sub("", ln) for ln in markdown.splitlines()]
    baris = ["" if _PEMISAH_HALAMAN.match(ln) else ln for ln in baris]
    # Logo LaTeX disatukan jadi kata. Nomor halaman hanya boleh dibuang pada
    # mode pembersihan lama; impor awal memakai preserve_content=True.
    keluar: list[str] = []
    for ln in baris:
        ln = _LOGO_LATEX.sub("LaTeX", ln)
        if not preserve_content and _HALAMAN_ONLY.match(ln.strip()):
            continue
        keluar.append(ln)
    # Listing kode bernomor yang disatukan pengekstrak jadi blok kode.
    if not preserve_content:
        keluar = _fence_listing(keluar)
    # Rapatkan baris kosong beruntun jadi paling banyak satu.
    hasil: list[str] = []
    for ln in keluar:
        if not ln.strip() and hasil and not hasil[-1].strip():
            continue
        hasil.append(ln.rstrip())
    return "\n".join(hasil).strip() + "\n"


def _fence_listing(baris: list[str]) -> list[str]:
    """Bungkus listing kode satu baris hasil impor PDF jadi blok kode bertanda."""
    keluar: list[str] = []
    for ln in baris:
        if len(ln) < 200 or len(_KODE_PECAH.findall(ln)) < 3:
            keluar.append(ln)
            continue
        if not _KODE_MARK.search(ln):
            keluar.append(ln)
            continue
        keluar.append("```")
        keluar.extend(_pulihkan_baris_kode(_KODE_PECAH.split(ln.strip())))
        keluar.append("```")
    return keluar



# Kata yang terputus oleh pemenggalan baris PDF: baris berakhir tanda hubung dan
# baris berikutnya dibuka huruf kecil. LaTeX memenggal kata saat menjustifikasi
# ("Do-\ncument"), sehingga penggabungan naif menyisakan "Do- cument". Terukur
# 356 kemunculan pada laporan uji 89 halaman.
_HUBUNG_PENGGAL = re.compile(r"(\w)-$")
_KATA_SEBELUM_HUBUNG = re.compile(r"([A-Za-zÀ-ÿ]+)-$")

# Kata majemuk yang tanda hubungnya ASLI dan kebetulan jatuh di ujung baris.
# Tanpa daftar ini "meta-analitik" ikut disatukan menjadi "metaanalitik".
# Penggalan lunak jauh lebih sering (terukur ~25:2 pada laporan uji), jadi
# default-nya menyatukan; hanya bentuk di bawah yang mempertahankan hubungnya.
_MAJEMUK_BERHUBUNG = frozenset({
    "meta", "tanya", "recency", "mastery", "retrieval", "context", "self",
    "non", "pra", "pasca", "anti", "multi", "antar", "sub", "pseudo", "semi",
    "co", "e", "in", "ex", "re", "pre", "post", "open", "closed", "end",
})


def _gabung_kata_terpenggal(kiri: str, kanan: str) -> str:
    """Sambung dua baris; kata yang terputus tanda hubung disatukan kembali."""
    if not _HUBUNG_PENGGAL.search(kiri) or not kanan[:1].islower():
        return f"{kiri} {kanan}"
    kata_kiri = _KATA_SEBELUM_HUBUNG.search(kiri)
    if kata_kiri and kata_kiri.group(1).lower() in _MAJEMUK_BERHUBUNG:
        # Hubungnya asli: rapatkan tanpa spasi ("meta-" + "analitik").
        return f"{kiri}{kanan}"
    return f"{kiri[:-1]}{kanan}"


def _sel_tabel_bersih(nilai: object) -> str:
    """Isi satu sel tabel PDF jadi satu baris, kata terpenggal disatukan.

    Sel bisa memuat beberapa baris tercetak; meratakannya dengan spasi apa adanya
    menyisakan kata pecah seperti "Diha- rapkan" (kepala tabel "Hasil yang
    Diharapkan"). Terukur 28 kemunculan di dalam tabel pada laporan uji.
    """
    teks = (str(nilai) if nilai is not None else "").replace("\r", "\n")
    baris = [b.strip() for b in teks.split("\n") if b.strip()]
    if not baris:
        return ""
    hasil = baris[0]
    for lanjut in baris[1:]:
        hasil = _gabung_kata_terpenggal(hasil, lanjut)
    return hasil.strip()


def _tabel_baris_ke_markdown(rows: list[list]) -> str | None:
    """Baris sel hasil `find_tables` → tabel pipa markdown; None bila noise.

    Detektor tabel PyMuPDF ikut menangkap kotak tata letak yang bukan tabel
    (kolom kosong, sel "None"). Tabel yang pantas minimal 2 baris, 2 kolom,
    dan memuat ≥ 4 sel berisi selain "None"; selain itu dibuang supaya impor
    tidak membanjiri draf dengan kotak kosong.
    """
    bersih: list[list[str]] = []
    for r in rows:
        sel = [_sel_tabel_bersih(c) for c in r]
        if any(sel):
            bersih.append(sel)
    if len(bersih) < 2:
        return None
    kolom = max(len(r) for r in bersih)
    if kolom < 2:
        return None
    isi = sum(1 for r in bersih for c in r if c and c.lower() != "none")
    if isi < 4:
        return None

    def baris_md(sel: list[str]) -> str:
        # "|" dalam sel memecah kolom tabel pipa → ganti dengan garis miring.
        sel = [c.replace("|", "/") for c in sel]
        return "| " + " | ".join(sel + [""] * (kolom - len(sel))) + " |"

    out = [baris_md(bersih[0]), "| " + " | ".join(["---"] * kolom) + " |"]
    out.extend(baris_md(r) for r in bersih[1:])
    return "\n".join(out)


def _adalah_heading(size: float, teks: str) -> bool:
    """Apakah baris PDF (ukuran font + bentuk teks) adalah heading?"""
    stripped = teks.strip()
    if size >= _HEADING_FONT_MIN:
        return True
    return (
        size >= 12
        and stripped.isupper()
        and 3 <= len(stripped) <= 60
        and " " in stripped
        and not stripped.endswith(".")
    )


def _baris_center(x0: float, x1: float, lebar_teks: float) -> bool:
    """Apakah baris PDF di-tengah (bukan rata kiri/justify penuh)?

    Judul halaman muka ("LEMBAR PENGESAHAN..."), judul bab ("Bab 1"), dan baris
    tanda tangan diketik di tengah halaman. Pembedanya terhadap baris justify
    BUKAN lebar, melainkan celah ke margin: baris justify berakhir TEPAT di kedua
    margin sehingga celahnya nol (bahkan sedikit negatif karena pembulatan bbox),
    sedangkan baris tengah menyisakan celah simetris di kiri dan kanan.

    Aturan lama menolak baris tengah yang lebih lebar dari 0,85 × lebar teks dan
    yang mulai <30 pt dari margin. Judul dokumen yang panjang ("NALAR AI:
    PENGEMBANGAN INTELLIGENT TUTORING" — 91% lebar teks, celah 17 pt) karena itu
    dinilai justify padahal terpusat sempurna, sehingga satu judul terpecah:
    sebagian barisnya masuk blok `<center>`, sebagian tidak.

    Uji simetri juga yang membedakan terhadap baris ber-indentasi menggantung
    (daftar pustaka, lanjutan butir) yang kebetulan berada di sekitar tengah —
    indentasinya tetap, tapi ujung kanannya berhenti sembarang.
    """
    margin_kiri = 4 * _CM_PT
    celah_kiri = x0 - margin_kiri
    celah_kanan = (margin_kiri + lebar_teks) - x1
    if min(celah_kiri, celah_kanan) <= 8:
        return False
    return abs(celah_kiri - celah_kanan) <= 6


# Penanda halaman sampul laporan akademik. Baris judul semata (tanpa penanda)
# tidak cukup — makalah biasa pun punya judul besar; logo + beberapa heading
# pendek menjadi petunjuk cadangan untuk sampul yang tidak menyebutkan kata kunci.
_CALON_COVER = re.compile(
    r"\b(LAPORAN|TUGAS AKHIR|SKRIPSI|TESIS|DISERTASI|PROPOSAL|PROYEK AKHIR)\b",
    re.I,
)


# Halaman yang langsung berisi bab ("BAB 1 ...", "1.1 ...") bukan sampul,
# meskipun memuat gambar dan beberapa heading — mis. laporan yang membuka
# langsung ke BAB 1 dengan figur di halaman pertamanya.
# `\d{1,3}\s+` menangkap judul bab bernomor tanpa kata "BAB" ("1 PENDAHULUAN"),
# tetapi tidak menolak tahun sampul "2026" (4 digit, berdiri sendiri).
_TERLARANG_COVER = re.compile(r"^(?:BAB\s+[\dIVX]+|\d+\.\d+|\d{1,3}\s+)\b", re.I)


def _halaman_cover(page_index: int, heading: list[str], punya_gambar: bool) -> bool:
    """Benarkah halaman pertama ini halaman sampul, bukan bab biasa?

    Sampul dikenali dari penanda ("LAPORAN TUGAS AKHIR", "SKRIPSI", ...) atau
    logo + minimal dua heading pendek. Halaman lain tidak pernah sampul, dan
    halaman yang langsung berisi BAB 1 tidak pernah sampul.
    """
    if page_index != 0 or not heading:
        return False
    if any(_TERLARANG_COVER.search(h) for h in heading):
        return False
    ada_tanda = any(_CALON_COVER.search(h) for h in heading)
    return ada_tanda or (punya_gambar and len(heading) >= 2)


def _tingkat_heading(teks: str) -> int:
    """Tingkat heading dari bentuk teks PDF, bukan hanya ukuran font.

    Ekstraktor hanya tahu ukuran font (semua heading jadi `##` di markdown),
    sehingga DOCX hasil impor rata-rata "Heading 2" dan daftar isi di editor
    kehilangan tingkatan. Pola bab/angka dipetakan agar struktur kembali:
    "BAB 1"/"Bab 1"→H1, "1.2 ..."→H2, "1.2.3 ..."→H3, sisanya tetap heading
    yang sudah dinilai dari ukuran font (H2 bila masih `##`).
    Halaman muka ("LEMBAR PENGESAHAN ...") juga H1 karena berdiri sebagai bab.
    """
    t = teks.strip()
    if _BAB.match(t) or _FRONT.match(t):
        return 1
    if _NOMOR_3.match(t):
        return 3
    if _NOMOR_2.match(t):
        return 2
    return 2


# Nomor bagian yang berdiri sendiri sebagai fragmen ("2.3", "3.1.2"). LaTeX
# mencetak nomor \subsection di kotak terpisah, sehingga PyMuPDF melaporkannya
# sebagai baris tersendiri pada y yang SAMA dengan judulnya.
_NOMOR_SAJA = re.compile(r"^\d+(?:\.\d+)*\.?$")


def _gabung_fragmen_sebaris(
    items: list[tuple[float, str, float, float, float]],
) -> list[tuple[float, str, float, float, float]]:
    """Satukan fragmen yang sebenarnya SATU baris visual (y sama).

    PyMuPDF memecah satu baris menjadi beberapa "line" bila ada jarak horizontal
    besar — mis. nomor `\\subsection` dalam kotaknya sendiri, atau label sampul
    yang nilainya diketik pada tab stop. Akibatnya judul bagian terpisah dari
    nomornya ("## 2.3" lalu "## Large Language Model ...", dua entri daftar isi;
    terukur 6 entri cacat pada laporan uji) dan label sampul terpecah ("NPM"
    lalu ": 613230021").

    Penggabungan sengaja DIBATASI pada dua bentuk yang tidak ambigu: fragmen kiri
    berupa nomor bagian, atau fragmen kanan yang dibuka titik dua. Jarak saja
    tidak bisa dipakai sebagai penanda — nomor bagian berjarak 14,4 pt dari
    judulnya, sedangkan dua kolom tanda tangan yang WAJIB tetap terpisah hanya
    berjarak 15,9 pt.
    """
    per_y: dict[float, list[tuple[float, str, float, float, float]]] = {}
    for it in items:
        kunci = next((k for k in per_y if abs(k - it[0]) <= 1.5), it[0])
        per_y.setdefault(kunci, []).append(it)

    hasil: list[tuple[float, str, float, float, float]] = []
    for baris in per_y.values():
        baris.sort(key=lambda x: x[3])
        aktif = list(baris[0])
        for lanjut in baris[1:]:
            kiri = str(aktif[1]).strip()
            kanan = str(lanjut[1]).strip()
            celah = lanjut[3] - aktif[4]
            if celah < 25 and (_NOMOR_SAJA.match(kiri) or kanan.startswith(":")):
                aktif[1] = f"{kiri} {kanan}"
                aktif[2] = max(aktif[2], lanjut[2])
                aktif[4] = lanjut[4]
            else:
                hasil.append(tuple(aktif))  # type: ignore[arg-type]
                aktif = list(lanjut)
        hasil.append(tuple(aktif))  # type: ignore[arg-type]
    return hasil


def _pdf_to_markdown_sederhana(doc, images_dir: str, base_url: str) -> str:
    """Cadangan: heading dari ukuran font, tabel dari find_tables, paragraf disambung.

    Memakai PyMuPDF langsung (bukan pymupdf4llm) supaya tetap cepat untuk
    dokumen besar, tetapi tabel dideteksi lewat `find_tables` per halaman dan
    dirender sebagai tabel markdown — impor PDF laporan 89 halaman: tabel
    berisi (bukan kotak kosong) tetap muncul, dengan tambahan ~8 detik.
    """
    peta_xref, images = _ekstrak_gambar(doc, images_dir, base_url)
    images_by_page: dict[int, list[str]] = {}
    for page_index, url in images:
        images_by_page.setdefault(page_index, []).append(url)

    md_lines: list[str] = []
    # Sampul (halaman 0) ditutup `</center>` yang sudah menyisipkan `\newpage`
    # di markdown_to_latex — halaman berikutnya tidak perlu penanda lagi.
    cover_halaman0 = False
    # Blok `<center>` dibuka/ditutup lintas baris (dan lintas halaman) agar baris
    # tengah yang berurutan menyatu, bukan satu blok per baris.
    dalam_center = False
    for page_index, page in enumerate(doc):
        items: list[tuple[float, str, float, float, float]] = []
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") == 1:
                continue  # gambar ditangani lewat get_image_info (punya posisi baca)
            for line in block.get("lines", []):
                line_text = ""
                max_size = 0.0
                for span in line.get("spans", []):
                    line_text += span.get("text", "")
                    max_size = max(max_size, span.get("size", 0))
                if line_text.strip():
                    bbox = line.get("bbox", [0, 0, 0, 0])
                    items.append((bbox[1], line_text, max_size, bbox[0], bbox[2]))

        # Lebar area teks (margin kampus: kiri 4 + kanan 3 cm) dipakai untuk
        # skala gambar (?w=) dan deteksi baris tengah.
        lebar_teks_pt = max(1.0, page.rect.width - 7 * _CM_PT)

        # Posisi baca gambar: digabung dengan teks memakai koordinat y, bukan
        # ditumpuk di atas halaman (dulu logo sampul muncul sebelum judul).
        # Lebar tampilan asli di PDF diubah ke fraksi \\textwidth (petunjuk ?w=)
        # supaya \\includegraphics tidak memakai lebar default 0.8 yang membuat
        # logo sampul tampak raksasa.
        penempatan: list[tuple[float, str]] = []
        try:
            for info in page.get_image_info(xrefs=True):
                url = peta_xref.get(int(info.get("xref") or -1))
                bbox = info.get("bbox")
                if url is None or not bbox:
                    continue
                rasio = min(0.98, max(0.05, (bbox[2] - bbox[0]) / lebar_teks_pt))
                penempatan.append((bbox[1], f"{url}?w={rasio:.2f}"))
        except Exception:  # noqa: BLE001 — posisi gambar bukan alasan impor gagal
            penempatan = []

        heading_halaman = [t.strip() for _, t, s, _, _ in items if _adalah_heading(s, t)]
        is_cover = _halaman_cover(page_index, heading_halaman, bool(penempatan))
        if page_index == 0:
            cover_halaman0 = is_cover

        # Deteksi tabel per halaman; teks di dalam bbox tabel dibuang supaya
        # isi sel tidak terduplikasi (find_tables mengekstraknya sendiri).
        tabel_md: list[tuple[float, str]] = []
        tabel_y: list[tuple[float, float]] = []
        try:
            for t in page.find_tables().tables:
                md = _tabel_baris_ke_markdown(t.extract())
                if not md:
                    continue
                y0, y1 = t.bbox[1], t.bbox[3]
                tabel_md.append((y0, md))
                tabel_y.append((y0, y1))
        except Exception:  # noqa: BLE001 — kegagalan deteksi bukan alasan impor gagal
            tabel_md, tabel_y = [], []

        def _dalam_tabel(y: float) -> bool:
            return any(y0 - 2 <= y <= y1 + 2 for y0, y1 in tabel_y)

        # Fragmen satu baris visual disatukan dulu (nomor bagian + judulnya,
        # label sampul + nilainya). Isi tabel dilewati: sel tabel juga berbagi y,
        # dan `find_tables` sudah mengekstraknya sendiri.
        items = [it for it in items if _dalam_tabel(it[0])] + _gabung_fragmen_sebaris(
            [it for it in items if not _dalam_tabel(it[0])]
        )

        # Baris halaman disimpan bersama penanda "di tengah" supaya baris tengah
        # yang BERURUTAN digabung menjadi SATU blok `<center>` di akhir. Satu blok
        # per baris membuat halaman pengesahan berisi blok tengah satu-baris yang
        # berselang-seling — persis keluhan "tidak rapi".
        halaman_lines: list[tuple[bool, str]] = []
        teks_urut = sorted(items, key=lambda x: x[0])
        gambar_urut = sorted(penempatan, key=lambda x: x[0])
        i_t, i_g = 0, 0
        kursor_tabel = 0
        gambar_terakhir: tuple[float, str] | None = None
        # Halaman baru dibuka bila halaman memuat heading di tengah (bagian
        # muka/"LEMBAR PENGESAHAN...", "Bab 1", dst.) — LaTeX tidak bisa tahu
        # batas halaman PDF asli, jadi impor menandainya eksplisit.
        punya_heading_center = False
        while i_t < len(teks_urut) or i_g < len(gambar_urut):
            if i_g < len(gambar_urut) and (
                i_t >= len(teks_urut) or gambar_urut[i_g][0] <= teks_urut[i_t][0]
            ):
                y, url = gambar_urut[i_g]
                # Watermark/logo yang berulang berdekatan tidak perlu digandakan.
                if not (
                    gambar_terakhir
                    and abs(gambar_terakhir[0] - y) < 3
                    and gambar_terakhir[1] == url
                ):
                    # Gambar sampul ikut ke dalam blok tengah halaman sampul.
                    halaman_lines.append((is_cover, f"![Gambar]({url})"))
                    gambar_terakhir = (y, url)
                i_g += 1
                continue
            gambar_terakhir = None
            y, teks, ukuran, x0, x1 = teks_urut[i_t]
            i_t += 1
            if _dalam_tabel(y):
                continue
            # Tabel yang puncaknya sudah lewat disisipkan di posisi semula.
            while kursor_tabel < len(tabel_md) and tabel_md[kursor_tabel][0] < y - 2:
                halaman_lines.append((False, ""))
                halaman_lines.append((False, tabel_md[kursor_tabel][1]))
                halaman_lines.append((False, ""))
                kursor_tabel += 1
            stripped = teks.strip()
            # Nomor halaman yang terisolasi di margin atas/bawah ("i", "ii",
            # "12") bukan konten — LaTeX memberi nomor sendiri. Hanya dibuang
            # bila di tepi halaman dan berukuran kecil (bukan heading).
            if (
                _HALAMAN_ONLY.match(stripped)
                and ukuran < _HEADING_FONT_MIN
                and (y < 0.15 * page.rect.height or y > 0.85 * page.rect.height)
            ):
                continue
            # Baris di tengah halaman (judul muka, judul bab, tanda tangan)
            # ditandai blok tengah supaya LaTeX mencetaknya di tengah, bukan
            # paragraf rata kiri. Seluruh isi halaman sampul dianggap tengah agar
            # menyatu jadi satu blok.
            di_tengah = is_cover or _baris_center(x0, x1, lebar_teks_pt)
            if _adalah_heading(ukuran, teks):
                tingkat_n = _tingkat_heading(stripped)
                tingkat = "#" * tingkat_n
                if di_tengah:
                    # Judul DOKUMEN dan nama institusi bukan struktur: laporan
                    # kampus mengulangnya di sampul dan di tiap lembar pengesahan,
                    # diketik beberapa baris visual. Sebagai heading, tiap baris
                    # jadi entri daftar isi sendiri — terukur 106 heading pada
                    # laporan uji, daftar isi penuh entri satu kata. Yang tetap
                    # heading hanyalah bagian bernama ("LEMBAR PENGESAHAN") dan
                    # bab, yaitu yang dinilai tingkat 1 oleh _tingkat_heading.
                    if tingkat_n == 1:
                        punya_heading_center = True
                        halaman_lines.append((True, f"{tingkat} {stripped}"))
                    else:
                        halaman_lines.append((True, f"**{stripped}**"))
                else:
                    halaman_lines.append((False, f"{tingkat} {stripped}"))
            # Baris yang bukan heading/tengah: paragraf biasa. Awalan penanda
            # Markdown di-escape supaya baris komentar kode ("# Perkakas ...")
            # di lampiran tidak tercipta menjadi heading H1 yang mengacaukan bab.
            elif di_tengah:
                halaman_lines.append((True, teks.rstrip()))
            else:
                halaman_lines.append((False, _lolos_awalan_markdown(teks.rstrip())))
        while kursor_tabel < len(tabel_md):
            halaman_lines.append((False, ""))
            halaman_lines.append((False, tabel_md[kursor_tabel][1]))
            halaman_lines.append((False, ""))
            kursor_tabel += 1

        # Gambar yang terdaftar di resource halaman tapi tidak terdeteksi
        # posisinya (mis. gambar inline) tetap disertakan di akhir halaman.
        sisa = [
            u
            for u in images_by_page.get(page_index, [])
            if not any(u in ln for _, ln in halaman_lines)
        ]
        if sisa:
            halaman_lines.append((False, ""))
            halaman_lines.extend((False, f"![Gambar]({u})") for u in sisa)
            halaman_lines.append((False, ""))

        if not is_cover and punya_heading_center and page_index > 0:
            # Bagian muka/awal bab: paksa halaman baru di LaTeX. Halaman
            # lanjutan (mis. halaman 2 kata pengantar) tidak memuat heading
            # tengah, jadi mengalir menyambung seperti aslinya.
            # Halaman pertama setelah sampul tidak perlu penanda — `</center>`
            # sampul sudah memberi `\newpage` (mencegah halaman kosong ganda).
            if not (page_index == 1 and cover_halaman0):
                halaman_lines.insert(0, (False, "<newpage>"))

        # Baris tengah yang berurutan dibungkus SATU blok `<center>`.
        for di_tengah_baris, teks_baris in halaman_lines:
            if di_tengah_baris and not dalam_center:
                md_lines.append("<center>")
                dalam_center = True
            elif not di_tengah_baris and dalam_center:
                md_lines.append("</center>")
                dalam_center = False
            md_lines.append(teks_baris)
        # Blok ditutup di akhir halaman: batas halaman PDF adalah batas blok
        # tengah yang bermakna (sampul, tiap lembar pengesahan). Membiarkannya
        # terbuka membuat sampul dan lembar pengesahan menyatu jadi satu blok.
        if dalam_center:
            md_lines.append("</center>")
            dalam_center = False

    merged: list[str] = []
    for ln in md_lines:
        if (
            merged
            and _gaya_heading(ln)
            and _gaya_heading(merged[-1])
            and _heading_lanjutan(merged[-1], ln)
        ):
            merged[-1] = merged[-1] + " " + _isi_heading(ln)
        else:
            merged.append(ln)

    # Sambung paragraf yang terpotong: baris badan teks yang tidak berakhir tanda
    # akhir kalimat, diikuti baris yang dimulai huruf kecil, hampir pasti satu
    # paragraf yang terpecah oleh pemenggalan baris di PDF.
    return "\n".join(_sambung_paragraf(merged))


# Kata yang memulai bagian halaman muka/struktur — baris berikutnya yang
# dimulai kata ini pasti heading baru, bukan lanjutan judul yang terpotong.
_AWAL_BAGIAN_UTUH = {
    "LEMBAR",
    "SURAT",
    "HALAMAN",
    "PEDOMAN",
    "KATA",
    "DAFTAR",
    "ABSTRAK",
    "ABSTRACT",
    "BAB",
    "BANDUNG",
    "PROGRAM",
    "SEKOLAH",
    "UNIVERSITAS",
    "LAPORAN",
    "PERSEMBAHAN",
    "PERNYATAAN",
    "LAMPIRAN",
    "MOTTO",
    "GLOSARIUM",
    "BIODATA",
    "RIWAYAT",
}


_HEADING_MD = re.compile(r"^(#{1,6})\s+(.*)$")


def _gaya_heading(baris: str) -> str | None:
    """Awalan markdown heading ("#", "##") bila `baris` adalah heading, else None."""
    m = _HEADING_MD.match(baris)
    return m.group(1) if m else None


def _isi_heading(baris: str) -> str:
    """Isi heading tanpa awalan pagar markdown."""
    m = _HEADING_MD.match(baris)
    return m.group(2) if m else baris


def _heading_lanjutan(sebelum: str, berikut: str) -> bool:
    r"""Apakah dua baris heading berurutan adalah SATU judul yang terpotong baris?

    Heading PDF yang benar-benar terpecah baris menyisakan penanda: tanda baca
    (koma/titik dua/tanda pisah) di akhir baris pertama, atau kalimat berlanjut
    huruf kecil. Judul pendek juga disambung bila hasilnya tetap pendek dan
    baris berikutnya bukan bagian baru ("Bab 1" + "PENDAHULUAN" → satu judul).

    Baris sampul yang berdiri sendiri ("BANDUNG 2026", "LEMBAR PENGESAHAN...",
    "PROGRAM DIPLOMA III...") tidak punya penanda itu dan wajib dianggap heading
    terpisah — kalau tidak, seluruh halaman sampul menumpuk jadi satu
    `\subsection*{...}` raksasa yang tidak mirip dokumen aslinya.
    """
    isi_sebelum = _isi_heading(sebelum).rstrip()
    isi_berikut = _isi_heading(berikut).lstrip()
    if not isi_sebelum or not isi_berikut:
        return False
    if isi_sebelum.endswith((",", ":", ";", "-", "–", "—")):
        return True
    # Judul yang terpotong di tengah kalimat berlanjut huruf kecil.
    if isi_berikut[:1].islower():
        return True
    # Judul pendek yang terpecah baris ("Bab 1" + "PENDAHULUAN") disambung
    # selama hasilnya tetap wajar dan baris berikutnya bukan bagian baru.
    if len(isi_sebelum) <= 24 and len(isi_sebelum) + 1 + len(isi_berikut) <= 60:
        kata_pertama = isi_berikut.split()[0].upper().strip(".:;,-–—")
        if kata_pertama not in _AWAL_BAGIAN_UTUH:
            return True
    return False


def _boleh_sambung(baris: str, berikut: str) -> bool:
    """Apakah `berikut` adalah lanjutan paragraf dari `baris`?"""
    if not baris.strip() or not berikut.strip():
        return False
    if baris.startswith("#") or berikut.startswith("#"):
        return False
    if baris.startswith(("<", "!", "-", "*", ">", "|", " ", "\t")):
        return False
    if berikut.startswith(("<", "!", "-", "*", ">", "|", " ", "\t")):
        return False
    if baris.rstrip().endswith((".", "!", "?", ":", ";")):
        return False
    # Lanjutan paragraf dimulai huruf kecil; huruf besar menandai kalimat baru.
    return berikut.lstrip()[:1].islower()



def _sambung_paragraf(baris: list[str]) -> list[str]:
    """Gabungkan baris yang terpecah oleh pemenggalan baris PDF."""
    hasil: list[str] = []
    i = 0
    while i < len(baris):
        sekarang = baris[i].rstrip()
        # Terus sambung selama baris sesudahnya masih lanjutan paragraf ini.
        while i + 1 < len(baris) and _boleh_sambung(sekarang, baris[i + 1]):
            sekarang = _gabung_kata_terpenggal(sekarang, baris[i + 1].strip())
            i += 1
        hasil.append(sekarang)
        i += 1
    return hasil


def _tabel_ke_markdown(table) -> list[str]:
    """Tabel DOCX → tabel pipa markdown (baris pertama jadi kepala tabel)."""
    baris: list[list[str]] = []
    for row in table.rows:
        sel = [
            " ".join(c.text.split()).replace("|", "\\|") or " " for c in row.cells
        ]
        if any(s.strip() for s in sel):
            baris.append(sel)
    if not baris:
        return []
    lebar = max(len(r) for r in baris)
    baris = [r + [" "] * (lebar - len(r)) for r in baris]
    keluar = ["| " + " | ".join(baris[0]) + " |",
              "| " + " | ".join(["---"] * lebar) + " |"]
    keluar += ["| " + " | ".join(r) + " |" for r in baris[1:]]
    return keluar


def _gambar_paragraf(paragraph, document, images_dir: str, base_url: str) -> list[str]:
    """URL gambar yang benar-benar tertanam pada paragraf ini.

    Ditelusuri lewat relasi r:embed di dalam a:blip, jadi setiap gambar melekat
    pada paragraf pemiliknya — bukan dibagi rata ke paragraf-paragraf awal.
    """
    from docx.oxml.ns import qn

    urls: list[str] = []
    for blip in paragraph._element.iter(qn("a:blip")):
        rid = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
        if not rid:
            continue
        try:
            part = document.part.related_parts[rid]
        except KeyError:
            continue
        nama = os.path.basename(str(part.partname))
        try:
            nama = _save_bytes(images_dir, nama, part.blob)
        except Exception:  # noqa: BLE001 — relasi eksternal tanpa isi
            continue
        urls.append(f"{base_url}/{nama}")
    return urls


def _inline_docx(paragraph) -> str:
    """Isi paragraf DOCX sebagai Markdown, dengan tebal/miring dipertahankan.

    `paragraph.text` menggabungkan seluruh run menjadi satu string polos, jadi
    memakainya berarti membuang setiap penekanan di naskah. Pada laporan uji itu
    151 run tebal dan 358 run miring yang hilang — istilah asing, nama variabel,
    dan penegasan di dalam paragraf semuanya keluar sebagai teks biasa, dan
    itulah sebagian dari "hasilnya tidak sama dengan laporan aslinya".

    Run bersebelahan dengan format sama digabung dulu: `**a****b**` bukan
    Markdown yang sah dan akan tampil sebagai bintang mentah, sedangkan
    `**ab**` benar.
    """
    potongan: list[tuple[bool, bool, str]] = []
    for run in paragraph.runs:
        teks = run.text or ""
        if not teks:
            continue
        tebal = bool(run.bold)
        miring = bool(run.italic)
        if potongan and potongan[-1][0] == tebal and potongan[-1][1] == miring:
            potongan[-1] = (tebal, miring, potongan[-1][2] + teks)
        else:
            potongan.append((tebal, miring, teks))

    keluar: list[str] = []
    for tebal, miring, teks in potongan:
        if not (tebal or miring) or not teks.strip():
            keluar.append(teks)
            continue
        # Spasi tepi harus di LUAR penanda: `** tebal **` tidak dikenali
        # Markdown sebagai tebal dan akan tercetak beserta bintangnya.
        depan = teks[: len(teks) - len(teks.lstrip())]
        belakang = teks[len(teks.rstrip()) :]
        inti = teks.strip()
        penanda = "**" if tebal else ""
        if miring:
            inti = f"*{inti}*"
        keluar.append(f"{depan}{penanda}{inti}{penanda}{belakang}")
    return "".join(keluar)


def _jeda_halaman_docx(paragraph) -> bool:
    """True bila paragraf ini dimulai di halaman baru menurut DOCX-nya.

    Dua penulisan yang dipakai Word: `w:br type="page"` di dalam run, dan
    `w:pageBreakBefore` di properti paragraf. Keduanya hilang seluruhnya pada
    ekstraksi berbasis `paragraph.text`, sehingga bab-bab yang di naskah asli
    mulai di halaman sendiri menyatu di tengah halaman — penyebab utama jumlah
    halaman hasil impor jauh lebih sedikit daripada laporan aslinya.
    """
    from docx.oxml.ns import qn

    for br in paragraph._p.iter(qn("w:br")):
        if br.get(qn("w:type")) == "page":
            return True
    ppr = paragraph._p.find(qn("w:pPr"))
    if ppr is not None and ppr.find(qn("w:pageBreakBefore")) is not None:
        return True
    return False


def docx_to_markdown(contents: bytes, images_dir: str, base_url: str, doc_id: str) -> str:
    """DOCX → markdown, mengikuti urutan elemen badan dokumen."""
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(contents))
    md_lines: list[str] = []
    terpakai: set[str] = set()

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:tbl"):
            md_lines.append("")
            md_lines.extend(_tabel_ke_markdown(Table(child, document)))
            md_lines.append("")
            continue
        if child.tag != qn("w:p"):
            continue

        para = Paragraph(child, document)
        # `<newpage>` adalah penanda yang sudah dipahami markdown_to_latex
        # (jadi \newpage), diletakkan SEBELUM isi paragrafnya.
        if _jeda_halaman_docx(para):
            md_lines.extend(["", "<newpage>", ""])
        for url in _gambar_paragraf(para, document, images_dir, base_url):
            md_lines.extend(["", f"![Gambar]({url})", ""])
            terpakai.add(url)

        teks = para.text.strip()
        if not teks:
            continue
        gaya = (para.style.name or "").lower()
        tingkat = 0
        if gaya.startswith("heading"):
            try:
                tingkat = int(gaya.replace("heading", "").strip() or "1")
            except ValueError:
                tingkat = 1
        elif "title" in gaya:
            tingkat = 1

        if tingkat:
            # Judul dibiarkan polos: penekanan di dalam heading tidak membawa
            # makna tambahan, dan `# **Bab I**` membuat outline ikut berbintang.
            md_lines.append("")
            md_lines.append(f"{'#' * min(tingkat, 6)} {teks}")
            md_lines.append("")
        elif gaya.startswith("list"):
            md_lines.append(f"- {_lolos_awalan_markdown(_inline_docx(para).strip())}")
        else:
            md_lines.append("")
            md_lines.append(_lolos_awalan_markdown(_inline_docx(para).strip()))

    # Gambar yang tidak tertaut paragraf mana pun (mis. di header/footer atau
    # kotak teks) tetap disertakan di akhir agar tidak hilang dari laporan.
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as zf:
            for name in zf.namelist():
                if not name.startswith("word/media/") or name.endswith("/"):
                    continue
                # Nama disimpan lebih dulu supaya perbandingan dengan `terpakai`
                # memakai nama setelah konversi (image3.wmf → image3.png);
                # kalau tidak, gambar yang sudah tertaut paragraf disisipkan
                # dua kali, yang kedua menunjuk berkas .wmf yang tidak ada.
                nama = _save_bytes(images_dir, os.path.basename(name), zf.read(name))
                url = f"{base_url}/{nama}"
                if url in terpakai:
                    continue
                terpakai.add(url)
                md_lines.extend(["", f"![Gambar]({url})", ""])
    except zipfile.BadZipFile:
        pass

    return _rapikan("\n".join(md_lines))
