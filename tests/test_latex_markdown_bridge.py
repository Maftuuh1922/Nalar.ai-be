"""Uji jembatan LaTeX → Markdown untuk ekspor DOCX/PDF.

Cakupan yang diuji adalah elemen yang benar-benar dihasilkan
`markdown_to_latex` dan dibaca `docx_template_exporter`: heading, format
inline, daftar, tabel, dan gambar. Termasuk uji putar-balik (markdown →
latex → markdown) karena itulah jalur nyata draf lama yang dikonversi lalu
diekspor kembali ke Word.
"""

from app.services.latex_export import (
    judul_dari_latex,
    latex_to_markdown,
    markdown_to_latex,
)


def test_heading_semua_tingkat():
    tex = "\n".join([
        r"\section{Pendahuluan}",
        r"\subsection{Latar Belakang}",
        r"\subsubsection{Rumusan}",
    ])
    md = latex_to_markdown(tex)
    assert "# Pendahuluan" in md
    assert "## Latar Belakang" in md
    assert "### Rumusan" in md


def test_format_inline():
    tex = r"Ada \textbf{tebal}, \textit{miring}, dan \texttt{kode} di sini."
    md = latex_to_markdown(tex)
    assert "**tebal**" in md
    assert "*miring*" in md
    assert "`kode`" in md


def test_inline_bersarang():
    """\\textbf{\\textit{x}} harus jadi **_x_**, bukan menyisakan perintah."""
    md = latex_to_markdown(r"\textbf{\textit{penting}}")
    assert "textit" not in md
    assert "textbf" not in md
    assert "penting" in md


def test_daftar_bullet_dan_bernomor():
    tex = "\n".join([
        r"\begin{itemize}",
        r"\item Satu",
        r"\item Dua",
        r"\end{itemize}",
        r"\begin{enumerate}",
        r"\item Pertama",
        r"\item Kedua",
        r"\end{enumerate}",
    ])
    md = latex_to_markdown(tex)
    assert "- Satu" in md
    assert "- Dua" in md
    assert "1. Pertama" in md
    assert "2. Kedua" in md


def test_tabel_tanpa_sisa_perintah_booktabs():
    """Perintah \\toprule/\\midrule tidak boleh bocor ke dalam sel."""
    tex = "\n".join([
        r"\begin{table}[h]",
        r"\centering",
        r"\begin{tabular}{ll}",
        r"\toprule",
        r"Kolom A & Kolom B \\",
        r"\midrule",
        r"Data 1 & Data 2 \\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
    ])
    md = latex_to_markdown(tex)
    assert "midrule" not in md
    assert "toprule" not in md
    assert "bottomrule" not in md
    assert "| Kolom A | Kolom B |" in md
    assert "| Data 1 | Data 2 |" in md
    # Baris pemisah header markdown harus ada, dengan jumlah kolom yang benar.
    assert "| --- | --- |" in md


def test_gambar_dengan_caption():
    tex = "\n".join([
        r"\begin{figure}[h]",
        r"\centering",
        r"\includegraphics[width=0.8\textwidth]{uploads/a/images/x.png}",
        r"\caption{Arsitektur sistem}",
        r"\end{figure}",
    ])
    md = latex_to_markdown(tex)
    assert "![Arsitektur sistem](uploads/a/images/x.png)" in md


def test_preamble_dibuang():
    tex = "\n".join([
        r"\documentclass[12pt,a4paper]{article}",
        r"\usepackage{graphicx}",
        r"\geometry{a4paper,top=4cm}",
        r"\begin{document}",
        r"Isi laporan.",
        r"\end{document}",
    ])
    md = latex_to_markdown(tex)
    assert "documentclass" not in md
    assert "usepackage" not in md
    assert md.strip() == "Isi laporan."


def test_karakter_khusus_dibatalkan_escapenya():
    tex = r"Nilai 50\% dan A\&B serta nama\_metode"
    md = latex_to_markdown(tex)
    assert "50%" in md
    assert "A&B" in md
    assert "nama_metode" in md


def test_blok_kode_jadi_fence():
    tex = "\n".join([
        r"\begin{verbatim}",
        "def f():",
        "    return 1",
        r"\end{verbatim}",
    ])
    md = latex_to_markdown(tex)
    assert md.count("```") == 2
    assert "def f():" in md
    # Indentasi di dalam blok kode dipertahankan.
    assert "    return 1" in md


def test_putar_balik_markdown_latex_markdown():
    """Draf lama: markdown → latex (konversi otomatis) → markdown (ekspor DOCX)."""
    asli = "\n".join([
        "# Bab I",
        "",
        "Paragraf dengan **tebal**.",
        "",
        "- Butir satu",
        "- Butir dua",
    ])
    hasil = latex_to_markdown(markdown_to_latex(asli))
    assert "# Bab I" in hasil
    assert "**tebal**" in hasil
    assert "- Butir satu" in hasil
    assert "- Butir dua" in hasil
    # Tidak ada perintah LaTeX yang tersisa.
    assert "\\section" not in hasil
    assert "\\item" not in hasil


def test_sampul_markdown_center_ke_latex():
    """Halaman sampul: heading jadi teks tebal besar di tengah, bukan subsection."""
    sumber = "\n".join([
        "<center>",
        "## NALAR AI: PENGEMBANGAN RUANG KERJA PENULISAN",
        "## LAPORAN TUGAS AKHIR",
        "Diajukan untuk memenuhi kelulusan",
        "</center>",
    ])
    tex = markdown_to_latex(sumber, preserve_source=True)
    assert r"\begin{center}" in tex
    assert r"\end{center}" in tex
    # Sampul ditutup dengan ganti halaman supaya pengesahan tidak ikut naik.
    assert r"\newpage" in tex
    assert r"\textbf{\large NALAR AI: PENGEMBANGAN RUANG KERJA PENULISAN}" in tex
    assert r"\textbf{\large LAPORAN TUGAS AKHIR}" in tex
    # Bukan heading bab yang rata kiri.
    assert r"\subsection*{LAPORAN TUGAS AKHIR}" not in tex
    assert "Diajukan untuk memenuhi kelulusan" in tex


def test_sampul_judul_tingkat_satu_lebih_besar():
    tex = markdown_to_latex("<center>\n# JUDUL UTAMA\n</center>")
    assert r"\textbf{\LARGE JUDUL UTAMA}" in tex


def test_gambar_petunjuk_lebar_dari_impor():
    """?w= (skala asli di PDF) dipakai, query dibuang dari path."""
    sumber = "![Gambar](http://x/uploads/d/images/img_001.png?w=0.18)"
    tex = markdown_to_latex(sumber, preserve_source=True)
    assert (
        r"\includegraphics[width=0.18\textwidth]{http://x/uploads/d/images/img_001.png}"
        in tex
    )
    assert "?w=" not in tex


def test_gambar_url_query_w_besar_tidak_dianggap_petunjuk():
    """?w=800 (query asli URL tulisan tangan) tidak boleh di-strip."""
    sumber = "![Gambar](http://x/a.png?w=800)"
    tex = markdown_to_latex(sumber, preserve_source=True)
    assert r"\includegraphics[width=0.8\textwidth]{http://x/a.png?w=800}" in tex


def test_center_tengah_dokumen_tidak_memaksa_halaman_baru():
    """Hanya blok center PERTAMA (sampul) yang ditutup \\newpage."""
    tex = markdown_to_latex("Paragraf biasa.\n\n<center>\n## Judul\n</center>\n")
    assert tex.count(r"\newpage") == 0
    assert tex.count(r"\begin{center}") == 1


def test_penanda_newpage_jadi_ganti_halaman():
    """<newpage> dari impor PDF menjadi \\newpage di LaTeX; penanda tepat
    setelah sampul tidak mengganda (sampul sudah memberi \\newpage)."""
    tex = markdown_to_latex("Paragraf biasa.\n<newpage>\n## BAB I\n")
    assert tex.count(r"\newpage") == 1
    assert r"\subsection{BAB I}" in tex
    # Penanda setelah `</center>` sampul: tidak ada `\newpage` ganda.
    tex2 = markdown_to_latex("<center>\n## NALAR AI\n</center>\n<newpage>\n## BAB I\n")
    assert tex2.count(r"\newpage") == 1


def test_heading_tengah_non_sampul_tetap_heading_di_center():
    """Heading tengah di halaman muka tetap \\subsection* (masuk outline) tapi
    dicetak di tengah — bukan \\textbf{\\large} yang kehilangan struktur."""
    sumber = "<center>\n## NALAR AI\n</center>\n<newpage>\n<center>\n## LEMBAR PENGESAHAN DOSEN PEMBIMBING\n</center>\n"
    tex = markdown_to_latex(sumber, preserve_source=True)
    assert r"\textbf{\large NALAR AI}" in tex
    # Heading tengah kedua → subsection* di dalam center.
    assert r"\subsection*{LEMBAR PENGESAHAN DOSEN PEMBIMBING}" in tex
    # Hanya sampul yang mematikan nomor halaman.
    assert tex.count(r"\thispagestyle{empty}") == 1


def test_center_tengah_tanpa_newpage_di_dalam_dokumen():
    """Blok center di tengah dokumen (bukan sampul) tanpa penanda halaman
    tidak memaksa ganti halaman."""
    tex = markdown_to_latex("Paragraf biasa.\n\n<center>\nDi Bandung, 2026\n</center>\n")
    assert tex.count(r"\newpage") == 0
    assert "Di Bandung, 2026" in tex


def test_sampul_mematikan_nomor_halaman():
    """Blok center pertama (sampul) memakai \\thispagestyle{empty} — PDF asli
    sampul tidak bernomor halaman."""
    tex = markdown_to_latex("<center>\n## NALAR AI\n</center>\n\n## BAB I\n")
    assert r"\begin{center}" in tex
    assert r"\thispagestyle{empty}" in tex
    # Nomor halaman di halaman BAB I tetap hidup.
    assert tex.count(r"\thispagestyle{empty}") == 1


def test_center_tengah_dokumen_tidak_mematikan_nomor_halaman():
    """Blok center yang bukan sampul (isi_awal sudah False) tetap bernomor."""
    tex = markdown_to_latex("Paragraf biasa.\n\n<center>\n## Judul\n</center>\n")
    assert r"\thispagestyle{empty}" not in tex


def test_sampul_putar_balik_untuk_docx():
    """Sampul markdown → latex → markdown: tidak ada perintah LaTeX yang bocor."""
    sumber = "<center>\n## NALAR AI\n![Gambar](u.png?w=0.18)\n</center>\n"
    md = latex_to_markdown(markdown_to_latex(sumber))
    assert "\\LARGE" not in md
    assert "\\begin{center}" not in md
    assert "\\subsection" not in md
    assert "**NALAR AI**" in md
    # Mode preserve tidak menambah caption, jadi alt kosong (perilaku lama).
    assert "![](u.png)" in md
    # \\thispagestyle{empty} yang ditambah untuk sampul tidak boleh bocor.
    assert "thispagestyle" not in md


def test_judul_dari_latex():
    assert judul_dari_latex(r"\section{Pendahuluan}") == "Pendahuluan"
    assert judul_dari_latex(r"  \subsection{Latar Belakang}  ") == "Latar Belakang"
    assert judul_dari_latex(r"\documentclass{article}") is None
    assert judul_dari_latex("# Markdown") is None
    assert judul_dari_latex("") is None


def test_impor_awal_tidak_menambah_nomor_heading_caption_atau_membuang_halaman():
    sumber = "\n".join([
        "# Pendahuluan",
        "",
        "Isi laporan tetap sama.",
        "",
        "12",
        "",
        "![Gambar](uploads/doc/images/logo.png)",
    ])

    tex = markdown_to_latex(sumber, preserve_source=True)

    assert r"\section*{Pendahuluan}" in tex
    assert "\n12\n" in tex
    assert r"\includegraphics[width=0.8\textwidth]{uploads/doc/images/logo.png}" in tex
    assert r"\caption{" not in tex


def test_latex_to_markdown_menandai_blok_center_dan_membuka_kurung_ukuran():
    """Blok \\begin{center} jadi <center> (mode Word), bukan teks `{...}` mentah."""
    sumber = (
        r"\documentclass{article}" + "\n"
        + r"\begin{document}" + "\n"
        + r"\begin{center}" + "\n"
        + r"{\Large NALAR AI: JUDUL}" + "\n"
        + "LAPORAN TUGAS AKHIR\n"
        + r"\end{center}" + "\n"
        + r"\section{Pendahuluan}" + "\n"
        + "Isi.\n"
        + r"\end{document}" + "\n"
    )
    md = latex_to_markdown(sumber)
    assert "<center>" in md
    assert "NALAR AI: JUDUL" in md
    assert "{Large" not in md
    assert "NALAR AI: JUDUL}" not in md

    # Putar-balik: markdown_to_latex memahami penanda dan kembali membungkusnya.
    latex = markdown_to_latex(md, preserve_source=True)
    assert r"\begin{center}" in latex
    assert r"\end{center}" in latex
