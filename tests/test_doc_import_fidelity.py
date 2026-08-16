import io

import pytest

from app.services.doc_import import (
    _deteksi_source_latex_pdf,
    _ext_images_from_bytes,
    _halaman_cover,
    _lolos_awalan_markdown,
    _save_bytes,
    _heading_lanjutan,
    _pdf_to_markdown_sederhana,
    _rapikan,
)


class _Page:
    def __init__(self, text="", images=None):
        self._text = text
        self._images = images if images is not None else [(7,)]

    def get_text(self):
        return self._text

    def get_images(self, full=True):
        return self._images


class _Document:
    def __init__(self, texts=None):
        self.pages = [_Page(t) for t in texts] if texts else [_Page(), _Page()]

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def extract_image(self, xref):
        assert xref == 7
        return {"ext": "png", "image": b"gambar-sama"}


def test_nomor_halaman_tidak_dibuang_pada_impor_awal():
    hasil = _rapikan("Judul\n12\nIsi berikutnya\n", preserve_content=True)

    assert hasil.splitlines() == ["Judul", "12", "Isi berikutnya"]


def test_heading_lanjutan_hanya_saat_penanda_kelanjutan():
    # Judul yang terpecah baris dengan koma di akhir baris pertama → digabung.
    assert _heading_lanjutan("## NALAR AI: PENGEMBANGAN RUANG KERJA PENULISAN,", "## JURNAL BERBANTUAN AI DENGAN")
    # Lanjutan huruf kecil → satu paragraf judul yang terpotong.
    assert _heading_lanjutan("## PENGEMBANGAN", "## ruang kerja penulisan")
    # Baris sampul yang berdiri sendiri → heading terpisah, bukan digabung.
    assert not _heading_lanjutan("## BANDUNG 2026", "## LEMBAR PENGESAHAN DOSEN PEMBIMBING")
    assert not _heading_lanjutan("## LEMBAR PENGESAHAN", "## LAPORAN TUGAS AKHIR")
    assert not _heading_lanjutan("## PROGRAM DIPLOMA III", "## SEKOLAH TEKNOLOGI INFORMASI")
    # Judul pendek yang terpecah baris tetap disambung jadi satu judul.
    assert _heading_lanjutan("## Bab 1", "## PENDAHULUAN")
    assert _heading_lanjutan("## Bab 1 PENDAHULUAN", "## 1.1")
    assert _heading_lanjutan("## 1.1", "## Latar Belakang")
    # Baris kosong tidak pernah digabung.
    assert not _heading_lanjutan("## Judul", "")
    assert not _heading_lanjutan("", "## Lanjutan")


def test_gambar_berulang_disimpan_sekali_tetapi_semua_kemunculan_dicatat(tmp_path):
    hasil = _ext_images_from_bytes(
        _Document(),
        str(tmp_path),
        "http://localhost/uploads/doc/images",
        "doc",
    )

    assert hasil == [
        (0, "http://localhost/uploads/doc/images/img_001.png"),
        (1, "http://localhost/uploads/doc/images/img_001.png"),
    ]
    assert (tmp_path / "img_001.png").read_bytes() == b"gambar-sama"


def test_deteksi_source_latex_pdf_tolak_source_mentah_menyeluruh():
    # Source .tex yang dicetak: `\{}` muncul di hampir semua halaman.
    doc = _Document(
        ["\\{}begincenter \\{}large \\{}small judul\\{}par \\{}endcenter"] * 10
    )

    with pytest.raises(ValueError, match="source LaTeX mentah"):
        _deteksi_source_latex_pdf(doc)


def test_deteksi_source_latex_pdf_tolak_bila_total_penanda_banyak():
    # 40 halaman, masing-masing hanya 3 penanda (< ambang 5/halaman) tapi
    # totalnya 120 >= 100 → tetap ditolak sebagai source mentah menyeluruh.
    doc = _Document(["\\{}begincenter \\{}judul\\{}par"] * 40)

    with pytest.raises(ValueError, match="source LaTeX mentah"):
        _deteksi_source_latex_pdf(doc)


def test_deteksi_source_latex_pdf_terima_lampiran_kode():
    # Lampiran kode Python memuat string LaTeX hanya di halaman terakhir —
    # seperti laporan 62 halaman yang diuji: 6 halaman lampiran, sisanya bersih.
    teks_bersih = "Laporan ini membahas sistem pendukung penulisan."
    lampiran = "out.append(r\"\\{}begin{figure}[h]\")\nout.append(r\"\\{}end{figure}\")"
    doc = _Document([teks_bersih] * 56 + [lampiran] * 6)

    _deteksi_source_latex_pdf(doc)


def test_deteksi_source_latex_pdf_terima_dokumen_bersih():
    doc = _Document(["Laporan bersih tanpa kode LaTeX."] * 5)
    _deteksi_source_latex_pdf(doc)


def test_deteksi_source_latex_pdf_terima_fraksi_kecil():
    # 1 halaman berat dari 10 (10% < 40%) — mis. lampiran kode — diterima.
    berat = "\\{}begincenter \\{}large \\{}small judul\\{}par \\{}endcenter"
    doc = _Document(["Laporan bersih."] * 9 + [berat])
    _deteksi_source_latex_pdf(doc)


def test_halaman_cover_hanya_halaman_pertama_dengan_penanda():
    # Sampul TA dikenali dari penanda (LAPORAN TUGAS AKHIR) atau logo + judul.
    assert _halaman_cover(0, ["LAPORAN TUGAS AKHIR"], True)
    assert _halaman_cover(0, ["NALAR AI: JUDUL", "LAPORAN TUGAS AKHIR"], False)
    assert _halaman_cover(0, ["JUDUL PENELITIAN", "NAMA PENULIS"], True)
    # Halaman selain pertama tidak pernah sampul.
    assert not _halaman_cover(1, ["LAPORAN TUGAS AKHIR"], True)
    # Bab biasa tanpa penanda dan tanpa logo → bukan sampul.
    assert not _halaman_cover(0, ["PENDAHULUAN"], False)
    assert not _halaman_cover(0, ["PENDAHULUAN", "1.1 Latar Belakang"], False)
    # Halaman yang langsung berisi BAB 1 + gambar bukan sampul.
    assert not _halaman_cover(0, ["BAB 1 PENDAHULUAN", "1.1 Latar Belakang"], True)
    # Tahun sampul ("2026") tidak membuat halaman ditolak.
    assert _halaman_cover(0, ["LAPORAN TUGAS AKHIR", "BANDUNG 2026"], True)


def test_impor_pdf_gambar_mengikuti_posisi_baca_dan_sampul_di_center(tmp_path):
    """Logo di tengah halaman sampul muncul SETELAH judul, dengan skala kecil.

    Memakai PDF sungguhan (PyMuPDF): teks judul di atas, gambar kecil di tengah,
    teks program di bawah. Bug lama menaruh semua gambar di ATAS halaman.
    """
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "LAPORAN TUGAS AKHIR", fontsize=16)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30), False)
    page.insert_image(fitz.Rect(250, 300, 320, 350), stream=pix.tobytes("png"))
    page.insert_text((72, 600), "PROGRAM DIPLOMA III", fontsize=16)
    try:
        hasil = _pdf_to_markdown_sederhana(
            doc, str(tmp_path), "http://x/uploads/d/images"
        )
    finally:
        doc.close()

    assert hasil.startswith("<center>")
    assert hasil.rstrip().endswith("</center>")
    posisi_judul = hasil.index("## LAPORAN TUGAS AKHIR")
    posisi_gambar = hasil.index("![Gambar]")
    posisi_program = hasil.index("## PROGRAM DIPLOMA III")
    # Urutan baca asli: judul → logo → program (logo bukan di atas halaman).
    assert posisi_judul < posisi_gambar < posisi_program
    # Logo kecil di halaman → skala kecil (~0.17 dari \\textwidth), bukan
    # lebar default 0.8 yang membuat logo tampak raksasa.
    assert "?w=0.1" in hasil
    assert "?w=0.8" not in hasil


def test_heading_dan_baris_tengah_halaman_muka_dibungkus_blok_tengah(tmp_path):
    """Heading muka (LEMBAR PENGESAHAN) dan baris tanda tangan yang di tengah
    di PDF asli diketik di tengah halaman → ditandai blok tengah, bukan heading
    `\subsection*` rata kiri atau paragraf kiri."""
    fitz = pytest.importorskip("fitz")
    font = fitz.Font("helv")

    def teks_tengah(page, teks, y, fontsize=14):
        lebar = font.text_length(teks, fontsize=fontsize)
        x0 = 311.8 - lebar / 2  # pusat area teks (kiri 4 cm, kanan 3 cm)
        page.insert_text((x0, y), teks, fontsize=fontsize)

    doc = fitz.open()
    # Halaman 1: sampul dengan penanda (cover di tengah otomatis).
    p1 = doc.new_page()
    teks_tengah(p1, "LAPORAN TUGAS AKHIR", 100, 16)
    teks_tengah(p1, "NALAR AI: JUDUL PENELITIAN", 140, 16)
    # Halaman 2: heading muka di tengah + baris tanda tangan di tengah.
    p2 = doc.new_page()
    teks_tengah(p2, "LEMBAR PENGESAHAN DOSEN PEMBIMBING", 120)
    p2.insert_text((150, 300), "Isi paragraf biasa yang rata kiri.", fontsize=11)
    teks_tengah(p2, "Di Bandung, 2026", 400, 11)
    # Halaman 3: heading muka lain → bagian baru.
    p3 = doc.new_page()
    teks_tengah(p3, "LEMBAR PENGESAHAN DOSEN PENGUJI", 120)
    try:
        hasil = _pdf_to_markdown_sederhana(
            doc, str(tmp_path), "http://x/uploads/d/images"
        )
    finally:
        doc.close()

    # Heading muka dibungkus blok tengah, bukan subsection rata kiri. Bagian
    # muka ("LEMBAR PENGESAHAN ...") dinilai H1 oleh `_tingkat_heading` (cocok
    # `_FRONT`) — berdiri sebagai bagian utama, sejajar "BAB 1" di daftar isi.
    assert "<center>\n# LEMBAR PENGESAHAN DOSEN PEMBIMBING\n</center>" in hasil
    assert "<center>\n# LEMBAR PENGESAHAN DOSEN PENGUJI\n</center>" in hasil
    # Baris tanda tangan di tengah juga dibungkus blok tengah.
    assert "<center>\nDi Bandung, 2026\n</center>" in hasil
    # Paragraf biasa tetap polos (bukan di tengah).
    assert "Isi paragraf biasa yang rata kiri." in hasil
    # Halaman 3 adalah bagian baru → penanda halaman; halaman 2 (tepat setelah
    # sampul yang sudah memberi \\newpage) tidak.
    assert hasil.count("<newpage>") == 1
    # Sampul tetap dibungkus utuh di awal.
    assert hasil.startswith("<center>")


def test_baris_justify_penuh_tidak_dianggap_tengah(tmp_path):
    """Baris paragraf yang mengisi penuh lebar teks (justify) bukan baris
    tengah — hanya baris yang lebarnya jauh di bawah textwidth dan terpusat."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    # Baris penuh justify: x0 di margin kiri, x1 di margin kanan.
    page.insert_text((113.4, 120), "Paragraf yang mengisi penuh lebar halaman ini dan", fontsize=11)
    page.insert_text((113.4, 300), "1.1 Latar Belakang", fontsize=14)
    try:
        hasil = _pdf_to_markdown_sederhana(
            doc, str(tmp_path), "http://x/uploads/d/images"
        )
    finally:
        doc.close()

    assert "<center>" not in hasil
    assert "## 1.1 Latar Belakang" in hasil


def test_komentar_kode_di_paragraf_docx_tidak_menjadi_heading():
    r"""Komentar Python di lampiran pernah terbaca sebagai heading Markdown.

    Paragraf DOCX biasa diteruskan apa adanya ke Pandoc, jadi baris
    "# Kumpulkan teks dari aliran" menjadi H1 lalu `\section*` — laporan hasil
    impor mendapat bab palsu yang mengacaukan outline dan pemecahan per bab.
    """
    assert _lolos_awalan_markdown("# Kumpulkan teks dari aliran") == (
        r"\# Kumpulkan teks dari aliran"
    )
    assert _lolos_awalan_markdown("    # indentasi ikut dipertahankan") == (
        "    " + r"\# indentasi ikut dipertahankan"
    )
    assert _lolos_awalan_markdown("> kutipan") == r"\> kutipan"
    assert _lolos_awalan_markdown("| a | b |") == r"\| a | b |"
    assert _lolos_awalan_markdown("- butir") == r"\- butir"
    assert _lolos_awalan_markdown("1. butir") == r"\1. butir"


def test_teks_biasa_tidak_ikut_di_escape():
    """Escape hanya untuk penanda blok di awal baris, bukan tanda baca biasa."""
    for teks in (
        "Nilai a - b dihitung ulang.",
        "Suhu naik 1.5 derajat.",
        "C#Sharp bukan heading",
        "Rumus a|b tetap utuh",
        "Paragraf biasa saja.",
    ):
        assert _lolos_awalan_markdown(teks) == teks


def test_tebal_dan_miring_docx_bertahan_sebagai_markdown():
    """`paragraph.text` membuang penekanan; impor tidak boleh memakainya.

    Pada laporan uji, 151 run tebal dan 358 run miring hilang seluruhnya —
    istilah asing dan penegasan di dalam paragraf keluar sebagai teks biasa,
    dan itu sebagian dari "hasilnya tidak sama dengan laporan aslinya".
    """
    from app.services.doc_import import _inline_docx

    import docx

    d = docx.Document()
    p = d.add_paragraph()
    p.add_run("Metode ")
    p.add_run("Design Science").italic = True
    p.add_run(" dipilih karena ")
    p.add_run("terukur").bold = True
    p.add_run(".")

    assert _inline_docx(p) == "Metode *Design Science* dipilih karena **terukur**."


def test_run_berdampingan_format_sama_digabung():
    """`**a****b**` bukan Markdown sah dan tampil sebagai bintang mentah.

    Word memecah satu frasa tebal menjadi beberapa run karena alasan yang tak
    terlihat di layar (pemeriksa ejaan, revisi), jadi penggabungan wajib.
    """
    from app.services.doc_import import _inline_docx

    import docx

    d = docx.Document()
    p = d.add_paragraph()
    for bagian in ("Sis", "tem ", "Pakar"):
        p.add_run(bagian).bold = True

    assert _inline_docx(p) == "**Sistem Pakar**"


def test_spasi_tepi_run_tebal_diletakkan_di_luar_penanda():
    """`** tebal **` tidak dikenali Markdown; bintangnya ikut tercetak."""
    from app.services.doc_import import _inline_docx

    import docx

    d = docx.Document()
    p = d.add_paragraph()
    p.add_run("Bab ")
    p.add_run(" Pendahuluan ").bold = True
    p.add_run("dimulai.")

    assert _inline_docx(p) == "Bab  **Pendahuluan** dimulai."


def test_jeda_halaman_docx_menjadi_penanda_newpage():
    r"""Jeda halaman Word hilang total pada ekstraksi berbasis `.text`.

    Akibatnya bab yang di naskah asli mulai di halaman sendiri menyatu di
    tengah halaman — penyebab utama jumlah halaman hasil impor jauh lebih
    sedikit daripada laporan aslinya (terukur 59 vs 89 halaman). `<newpage>`
    adalah penanda yang sudah dipahami `markdown_to_latex` sebagai `\newpage`.
    """
    from docx.enum.text import WD_BREAK

    from app.services.doc_import import _jeda_halaman_docx, docx_to_markdown

    import docx

    d = docx.Document()
    d.add_paragraph("Akhir bab satu.")
    p = d.add_paragraph()
    p.add_run().add_break(WD_BREAK.PAGE)
    d.add_paragraph("Awal bab dua.")

    assert _jeda_halaman_docx(d.paragraphs[0]) is False
    assert _jeda_halaman_docx(d.paragraphs[1]) is True

    buf = io.BytesIO()
    d.save(buf)
    md = docx_to_markdown(buf.getvalue(), "img", "http://x/img", "diag")
    assert "<newpage>" in md
    assert md.index("Akhir bab satu.") < md.index("<newpage>") < md.index("Awal bab dua.")


def test_jeda_halaman_dari_pageBreakBefore_ikut_terbaca():
    """Word menulis jeda halaman dengan dua cara; keduanya harus terbaca."""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    from app.services.doc_import import _jeda_halaman_docx

    import docx

    d = docx.Document()
    p = d.add_paragraph("BAB II TINJAUAN PUSTAKA")
    ppr = p._p.get_or_add_pPr()
    ppr.append(OxmlElement("w:pageBreakBefore"))

    assert p._p.find(qn("w:pPr")) is not None
    assert _jeda_halaman_docx(p) is True


def test_judul_tetap_polos_walau_run_nya_tebal():
    """Heading Word hampir selalu tebal; `# **Bab I**` mengotori outline."""
    from app.services.doc_import import docx_to_markdown

    import docx

    d = docx.Document()
    h = d.add_heading("", level=1)
    h.add_run("BAB I PENDAHULUAN").bold = True

    buf = io.BytesIO()
    d.save(buf)
    md = docx_to_markdown(buf.getvalue(), "img", "http://x/img", "diag")
    assert "# BAB I PENDAHULUAN" in md
    assert "**BAB I PENDAHULUAN**" not in md


def test_wmf_dikonversi_ke_png_saat_disimpan(tmp_path):
    """WMF/EMF tidak bisa disematkan tectonic.

    Membiarkannya membuat SELURUH ekspor PDF gagal dengan "image inclusion
    failed", jadi konversinya terjadi di perbatasan impor dan nama berkas hasil
    dikembalikan agar tautan gambar di draf ikut menunjuk .png.
    """
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    # Nama .wmf dengan isi PNG: Pillow tetap membukanya lewat pengenalan isi,
    # jadi jalur konversinya teruji tanpa bergantung GDI Windows.
    nama = _save_bytes(str(tmp_path), "image3.wmf", buf.getvalue())

    assert nama == "image3.png"
    assert (tmp_path / "image3.png").exists()
    assert not (tmp_path / "image3.wmf").exists()


def test_format_gambar_didukung_tidak_diubah(tmp_path):
    nama = _save_bytes(str(tmp_path), "image1.png", b"bukan-png-sungguhan")

    assert nama == "image1.png"
    assert (tmp_path / "image1.png").read_bytes() == b"bukan-png-sungguhan"
