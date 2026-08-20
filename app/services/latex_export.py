"""Konversi laporan markdown → LaTeX (PRD v2.5 §8) + compile PDF via tectonic.

- Heading #/##/### → \\section/\\subsection/\\subsubsection
- Bold/italic → \\textbf/\\textit
- Tabel markdown → tabular (booktabs)
- Gambar → figure + caption "Gambar N"
- List bullet/numbered → itemize/enumerate
- Blok kode → verbatim/listing
- Sitasi [n] dibiarkan sebagai teks (daftar pustaka manual di bagian akhir)
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from urllib.parse import unquote, urlparse

_TECTONIC = r"C:\Users\Administrator\Documents\project ta\bin\tectonic.exe"

# Batas waktu kompilasi tectonic. Laporan TA utuh (bab + gambar + daftar
# pustaka) di mesin pengembangan butuh sekitar satu menit; bila halaman
# pratinjau dan ekspor berjalan berdampingan, total bisa melewati 120 detik.
# Dipakai juga sebagai pesan galat yang terbaca, bukan 500 kosong.
_TECTONIC_TIMEOUT_SECONDS = 300

_PREAMBLE = r"""\documentclass[12pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{geometry}
\usepackage{setspace}
\usepackage{hyperref}
% Margin template kampus: atas 4 / bawah 3 / kiri 4 / kanan 3 cm — sama dengan
% chromium_pdf.MARGIN dan docx_template_exporter, supaya ketiga jalur ekspor
% (PDF LaTeX, PDF Chromium, DOCX) menghasilkan tata letak yang seragam.
\geometry{a4paper,top=4cm,bottom=3cm,left=4cm,right=3cm}
\onehalfspacing
\title{}
\date{}
\begin{document}
"""

_POSTAMBLE = r"""
\end{document}
"""


def _escape(text: str) -> str:
    """Escape karakter LaTeX khusus + ganti non-ASCII ke aman."""
    # Sisa penanda HTML dari impor: jadi spasi, bukan teks mentah — tanpa ini
    # kata di kiri dan kanannya menempel (mis. "atau" + "ON" -> "atauON").
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    # Karakter non-ASCII yang umum bermasalah di LaTeX lama (tanpa fontspec)
    text = text.replace("—", "--").replace("–", "-").replace("…", "...")
    text = text.replace("“", "\"\"").replace("”", "\"\"").replace("‘", "'").replace("’", "'")
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
        "<": r"\textless{}",
        ">": r"\textgreater{}",
        # Kurung siku dikurung agar tidak terbaca sebagai argumen opsional.
        # Sel tabel yang diawali "[n]" membuat "\\[n]" dibaca sebagai perintah
        # spasi vertikal dan LaTeX berhenti dengan "Missing number".
        # Ditaruh terakhir supaya kurung kurawal ini tidak ikut di-escape.
        "[": r"{[}",
        "]": r"{]}",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    # Simbol matematika + pangkat: diganti SETELAH $-nya di-escape, jadi
    # hasilnya math yang sah (tanpa ini hurufnya hilang: "Missing character").
    for k, v in {
        "≤": r"$\le$", "≥": r"$\ge$", "≠": r"$\ne$",
        "≪": r"$\ll$", "≫": r"$\gg$",
        "×": r"$\times$", "÷": r"$\div$", "±": r"$\pm$", "∞": r"$\infty$",
        "∆": r"$\Delta$", "∇": r"$\nabla$",
        "¹": r"\textsuperscript{1}", "²": r"\textsuperscript{2}", "³": r"\textsuperscript{3}",
    }.items():
        text = text.replace(k, v)
    # Panah dikerjakan SETELAH $-nya di-escape, jadi hasilnya math yang sah.
    text = text.replace("→", r"$\rightarrow$").replace("←", r"$\leftarrow$")
    return text


def escape_latex_text(text: str) -> str:
    """Escape plain text for use inside normal LaTeX document content."""
    return _escape(text or "")


# Penanda placeholder: bagian teks yang sudah menjadi perintah LaTeX
# (mengandung \\ dan {}) dilindungi dari _escape lewat token ini.
_PH = "\x00"
_PH_TOKEN = re.compile(f"{_PH}(\\d+){_PH}")


# Tag HTML yang disisipkan pengekstrak PDF (pymupdf4llm) untuk teks berformat.
_TAG_PASANGAN = [
    (r"<sup>", r"</sup>", r"\textsuperscript{"),
    (r"<sub>", r"</sub>", r"\textsubscript{"),
    (r"<u>", r"</u>", r"\underline{"),
    (r"<b>", r"</b>", r"\textbf{"),
    (r"<i>", r"</i>", r"\textit{"),
]
# Isi tag: teks biasa atau tag lain (untuk tag bersarang), bukan newline.
_TAG_ISI = r"((?:<[^>]+>[^<]*|[^<])*?)"

# Logo LaTeX hasil impor PDF: "L<sup>A</sup> TEX" (A bisa dibungkus penanda
# markdown, mis. "<sup>**A**</sup>"). Disatukan jadi kata "LaTeX" agar tidak
# tercetak terpotong "L" "TEX" dengan pangkat aneh.
_LOGO_LATEX = re.compile(r"L<sup>[^<]*</sup>\s*TEX", re.I)


def _inline(text: str) -> str:
    """Markdown inline → LaTeX: **bold**, *italic*, _italic_, `code`, dan tag
    HTML hasil impor PDF, lalu sanitasi karakter. Perintah LaTeX yang dihasilkan
    dilindungi lewat placeholder supaya kurung kurawalnya tidak ikut di-escape."""
    terlindungi: list[str] = []

    def lindungi(cmd: str) -> str:
        token = f"{_PH}{len(terlindungi)}{_PH}"
        terlindungi.append(cmd)
        return token

    # Logo LaTeX dinormalisasi sebelum tag diproses.
    text = _LOGO_LATEX.sub("LaTeX", text)
    # Tag HTML (dari dalam): <x>...</x> → \cmd{...}. Isi di-render penuh lewat
    # _inline rekursif, sehingga penanda markdown dan tag bersarang di
    # dalamnya (mis. <sup>**A**</sup>) tetap diproses dengan benar.
    for buka, tutup, cmd in _TAG_PASANGAN:
        text = re.sub(
            re.compile(re.escape(buka) + _TAG_ISI + re.escape(tutup), re.I),
            lambda m, cmd=cmd: lindungi(cmd) + lindungi(_inline(m.group(1))) + lindungi("}"),
            text,
        )
    text = re.sub(r"<mark>|</mark>", "", text, flags=re.I)
    # Markdown: tebal/miring/kode. Isinya boleh berisi token placeholder.
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: lindungi(r"\textbf{") + m.group(1) + lindungi("}"), text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", lambda m: lindungi(r"\textit{") + m.group(1) + lindungi("}"), text)
    # Italic markdown bergaris bawah (_teks_) dari pengekstrak PDF. Dikerjakan
    # setelah ** sehingga "_x_" di dalam kalimat tebal ikut terbungkus; kalau
    # "_" tersisa (mis. nama metode), _escape akan menjadikannya \_ yang aman.
    text = re.sub(
        r"(?<![A-Za-z0-9_])_(?!_)(.+?)_(?![A-Za-z0-9_])",
        lambda m: lindungi(r"\textit{") + m.group(1) + lindungi("}"),
        text,
    )
    text = re.sub(r"`([^`]+)`", lambda m: lindungi(r"\texttt{") + m.group(1) + lindungi("}"), text)
    text = _escape(text)
    return _PH_TOKEN.sub(lambda m: terlindungi[int(m.group(1))], text)


def _table_to_latex(lines: list[str]) -> str:
    """Markdown table → tabular."""
    rows = [re.sub(r"^\||\|$", "", ln).split("|") for ln in lines]
    rows = [[c.strip() for c in row] for row in rows]
    if not rows:
        return ""
    ncols = len(rows[0])
    colspec = "l" * ncols
    out = [r"\begin{table}[h]", r"\centering", r"\begin{tabular}{" + colspec + "}"]
    out.append(r"\toprule")
    for i, row in enumerate(rows):
        cells = " & ".join(_inline(c) for c in row)
        out.append(cells + r" \\")
        if i == 0:
            out.append(r"\midrule")
    out.append(r"\bottomrule")
    out.append(r"\end{tabular}")
    out.append(r"\end{table}")
    return "\n".join(out)


# ── Daftar isi hasil impor PDF ────────────────────────────────────────────────
# Pengekstrak PDF mengenali halaman daftar isi sebagai tabel dua kolom
# (nomor bagian | judul + titik pengisi + halaman). Bila dibiarkan, tabel itu
# tercetak bergaris — padahal daftar isi seharusnya baris judul-titik-halaman.
_LEADER = re.compile(r"^(?P<judul>.*?\S)\s*(?:\.\s*){4,}\s*(?P<hal>[ivxlcdm\d]*)\s*$", re.I)
_TOC_LABEL = re.compile(r"^(?:bab\s+[ivxlcdm\d]+|lampiran\s+\w|\d+(?:\.\d+)*)$", re.I)
_PEMISAH_TABEL = re.compile(r"^\|?[\s:|-]*-{2,}[\s:|-]*\|?$")
# Nomor halaman yang terlempar ke barisnya sendiri oleh pengekstrak PDF.
_HALAMAN_ONLY = re.compile(r"^(?:[ivxlcdm]{1,7}|\d{1,3})$", re.I)

# ── Listing kode lampiran hasil impor PDF ────────────────────────────────────
# Pengekstrak PDF menyatukan baris-baris kode bernomor menjadi SATU paragraf
# panjang ("1 def f() 2 return x"), tanpa fence ```. Di ekspor LaTeX baris itu
# jadi teks ter-escape yang rusak (dan bisa mematahkan compile). Deteksi khas:
# panjang > 200 karakter, minimal tiga awalan "N " (nomor baris), dan ada
# penanda kode (def/import/self./""" / return/lambda/print(...)).
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


def _deteksi_listing_kode(line: str) -> bool:
    """Benarkah paragraf ini listing kode yang disatukan pengekstrak?"""
    if len(line) < 200:
        return False
    if len(_KODE_PECAH.findall(line)) < 3:
        return False
    return bool(_KODE_MARK.search(line))


def _pecah_baris_kode(line: str) -> list[str]:
    """Pecah listing satu baris jadi baris-baris aslinya memakai nomor baris."""
    return _pulihkan_baris_kode(_KODE_PECAH.split(line.strip()))


def _gabung_sel(sel: list[str]) -> str:
    """Satukan sel-sel satu baris daftar isi jadi satu judul.

    Pengekstrak tata letak kerap memotong kolom di tengah kata
    ("DAFTA" | "R TABEL . . . xiv"), jadi penggabungan hanya diberi spasi bila
    sel pertama memang label bagian ("Bab 1", "1.1") — bukan penggalan kata.
    """
    sel = [c for c in sel if c]
    if not sel:
        return ""
    hasil = sel[0]
    for c in sel[1:]:
        if hasil.endswith(".") and c[:1].isdigit():
            hasil += c
        elif _TOC_LABEL.match(hasil.strip()) or hasil.endswith((".", " ")):
            hasil = f"{hasil} {c}"
        else:
            hasil += c
    return re.sub(r"\s{2,}", " ", hasil).strip()


def _tabel_toc_ke_latex(lines: list[str]) -> str | None:
    """Tabel markdown berisi daftar isi → baris tanpa garis, judul-titik-halaman.

    Return None bila tabel ini bukan daftar isi (tetap diproses sebagai tabel
    biasa oleh pemanggil).
    """
    isi = [ln for ln in lines if not _PEMISAH_TABEL.match(ln.strip())]
    if len(isi) < 2:
        return None
    entri: list[tuple[str, str]] = []
    for baris in isi:
        sel = [c.replace("<br>", " ").strip() for c in baris.strip().strip("|").split("|")]
        m = _LEADER.match(_gabung_sel(sel))
        if not m or not m.group("hal").strip():
            return None
        entri.append((m.group("judul").strip(), m.group("hal").strip()))
    baris_latex = [
        r"\noindent " + _inline(judul) + r" \dotfill " + _inline(hal) + r" \\"
        for judul, hal in entri
    ]
    return "\n".join([r"\begingroup\parindent=0pt"] + baris_latex + [r"\endgroup"])


def judul_dari_latex(line: str) -> str | None:
    """Ekstrak teks judul dari perintah LaTeX sectioning.

    Menangani \\section{...}, \\subsection{...}, \\subsubsection{...}, dan
    \\chapter{...}. Dipakai oleh rute Co-Writer yang perlu mendeteksi heading
    (outline, gap-analysis, media) setelah draf jadi LaTeX murni.

    Return teks judul (tanpa escape LaTeX), atau None bila bukan heading.
    """
    m = re.match(
        r"\\(?:chapter|section|subsection|subsubsection)\*?\s*\{([^}]*)\}",
        line.strip(),
    )
    if not m:
        return None
    # Kembalikan teks mentah: pemanggil tidak perlu unescape karena teks yang
    # dikembalikan hanya untuk perbandingan/outline, bukan rendering.
    return m.group(1).strip()


# ── LaTeX → Markdown ─────────────────────────────────────────────────────────
# Kebalikan terbatas dari markdown_to_latex, dipakai HANYA sebagai jembatan
# menuju pengekspor yang membaca Markdown (docx_template_exporter, typeset).
# Cakupannya sengaja sempit: elemen yang benar-benar dihasilkan
# markdown_to_latex — heading, gambar, tabel, daftar, kode, format inline.
# Untuk PDF, jalur yang benar tetap compile_latex_pdf, bukan lewat sini.

_TINGKAT_HEADING = {
    "chapter": 1,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
}

# Perintah preamble/tata letak yang tidak punya padanan Markdown.
_ABAIKAN_AWALAN = (
    r"\documentclass", r"\usepackage", r"\geometry", r"\title", r"\author",
    r"\date", r"\maketitle", r"\begin{document}", r"\end{document}",
    r"\onehalfspacing", r"\setstretch", r"\centering", r"\begin{figure}",
    r"\end{figure}", r"\begin{center}", r"\end{center}", r"\begingroup",
    r"\endgroup", r"\toprule", r"\midrule", r"\bottomrule", r"\hline",
    r"\newpage", r"\clearpage", r"\tableofcontents",
    # Sampul hasil impor mematikan nomor halaman; untuk DOCX itu tak ada
    # padanannya, jadi barisnya dibuang seperti \newpage.
    r"\thispagestyle",
)

_HEADING_TEX = re.compile(
    r"^\\(chapter|section|subsection|subsubsection|paragraph)\*?\s*\{(.*)\}\s*$"
)
_GAMBAR_TEX = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
_CAPTION_TEX = re.compile(r"\\caption\{(.*)\}")
# Baris daftar isi: "\noindent Judul \dotfill 12 \\"
_DOTFILL_TEX = re.compile(r"^\\noindent\s+(.*?)\s*\\dotfill\s*(.*?)\s*(?:\\\\)?$")

# Kebalikan tabel _escape: dikerjakan dari yang paling panjang supaya
# "\textbackslash{}" tidak terpotong jadi "\text" + sisa.
_UNESCAPE = [
    (r"\textbackslash{}", "\\"),
    (r"\textasciitilde{}", "~"),
    (r"\textasciicircum{}", "^"),
    (r"\textless{}", "<"),
    (r"\textgreater{}", ">"),
    (r"{[}", "["),
    (r"{]}", "]"),
    (r"\&", "&"),
    (r"\%", "%"),
    (r"\#", "#"),
    (r"\_", "_"),
    (r"\{", "{"),
    (r"\}", "}"),
    (r"\$", "$"),
]


def _inline_ke_markdown(text: str) -> str:
    """Perintah LaTeX inline → penanda Markdown, lalu batalkan escape.

    Perintah bersarang (mis. \\textbf{\\textit{x}}) diproses berulang sampai
    tidak ada lagi yang cocok; tanpa itu hanya lapisan terluar yang terkonversi
    dan sisanya tercetak sebagai perintah mentah di DOCX.
    """
    # Perintah ukuran font (judul sampul hasil blok <center>) tidak punya padanan
    # Markdown — dibuang supaya tidak tercetak mentah ("\\LARGE ...") di DOCX.
    # Kelompok `{\\Large isi}` (dipakai halaman sampul) dibuka kurungnya
    # sekaligus — brace tidak punya arti di Markdown dan membuat teks tampil
    # `{...}` saat diedit ala Word. Hanya yang kurung bukanya di awal baris;
    # `\textbf{\large X}` tidak boleh terpotong jadi `\textbfX`.
    text = re.sub(
        r"^\s*\{\\(?:Huge|huge|LARGE|Large|large|normalsize|small|footnotesize|"
        r"scriptsize|tiny)(?!\w)\s*([^{}]*)\}\s*",
        r"\1",
        text,
    )
    text = re.sub(
        r"\\(?:Huge|huge|LARGE|Large|large|normalsize|small|footnotesize|"
        r"scriptsize|tiny)(?!\w)\s*",
        "",
        text,
    )
    # Superscript/subscript (mis. logo LaTeX `L\textsuperscript{\textbf{A}} TEX`)
    # diambil isinya TANPA format dalam — kalau dibiarkan, `\textbf{A}` di
    # dalamnya menjadi `**A**` dan bergandengan dengan penanda `**` luar jadi
    # `****A**` yang merusak parsing tebal/miring di pengekspor DOCX.
    text = re.sub(
        r"\\(?:textsuperscript|textsubscript|textnormal)\{"
        r"((?:[^{}]|\{[^{}]*\})*)\}",
        lambda m: re.sub(
            r"\\(?:textbf|textit|emph|texttt|text|underline)\*?\{([^{}]*)\}",
            r"\1",
            m.group(1),
        ),
        text,
    )
    # Makro logo TeX umum → teks biasa.
    text = text.replace(r"\LaTeXe", "LaTeXe")
    text = text.replace(r"\LaTeX", "LaTeX")
    text = text.replace(r"\TeX", "TeX")
    def _flatten(isi: str) -> str:
        # `\textbf{\textit{X} Y}` menghasilkan `***X* Y**` yang ambigu bagi
        # parser DOCX (pasangan `**` dan `*` yang menempel tidak bisa dibedakan
        # dari `***X***`). Emphasis dalam diratakan ke gaya luar: `**X Y**`.
        return isi.replace("*", "").replace("`", "")

    for _ in range(4):  # kedalaman bersarang yang wajar; menghindari loop tak henti
        sebelum = text
        text = re.sub(
            r"\\textbf\{([^{}]*)\}",
            lambda m: f"**{_flatten(m.group(1))}**",
            text,
        )
        text = re.sub(r"\\textit\{([^{}]*)\}", r"*\1*", text)
        text = re.sub(r"\\emph\{([^{}]*)\}", r"*\1*", text)
        text = re.sub(r"\\texttt\{([^{}]*)\}", r"`\1`", text)
        # Tanpa padanan Markdown: perintahnya dibuang, isinya dipertahankan.
        text = re.sub(
            r"\\(?:underline|mbox|text)\{([^{}]*)\}",
            r"\1",
            text,
        )
        if text == sebelum:
            break
    # Sisa perintah tata letak dalam baris.
    text = text.replace(r"\noindent", "").replace(r"\dotfill", " ")
    # Line break LaTeX di akhir baris.
    text = re.sub(r"\\\\\s*$", "", text)
    for tex, asli in _UNESCAPE:
        text = text.replace(tex, asli)
    return text.strip()


def _tabular_ke_markdown(baris: list[str]) -> str:
    """Baris-baris tabular (sel dipisah &, baris diakhiri \\\\) → tabel Markdown."""
    tabel: list[list[str]] = []
    for b in baris:
        b = re.sub(r"\\\\\s*$", "", b.strip())
        if not b:
            continue
        tabel.append([_inline_ke_markdown(sel) for sel in b.split("&")])
    if not tabel:
        return ""
    kolom = max(len(r) for r in tabel)

    def _baris_md(sel: list[str]) -> str:
        return "| " + " | ".join(sel + [""] * (kolom - len(sel))) + " |"

    out = [_baris_md(tabel[0]), "| " + " | ".join(["---"] * kolom) + " |"]
    out.extend(_baris_md(r) for r in tabel[1:])
    return "\n".join(out)


def latex_to_markdown(latex_source: str) -> str:
    """Source LaTeX → Markdown untuk pengekspor yang belum paham LaTeX.

    Bukan konverter LaTeX umum: yang ditangani adalah keluaran
    `markdown_to_latex` dan tulisan tangan yang memakai perintah yang sama.
    Perintah di luar cakupan dibuang beserta baris tata letaknya, bukan
    dicetak mentah — teks seperti "\\begin{table}[h]" di dalam DOCX lebih
    membingungkan daripada tabel yang hilang.
    """
    baris = (latex_source or "").split("\n")
    out: list[str] = []
    tabel_buffer: list[str] | None = None
    nomor_enumerate = 0
    i = 0

    while i < len(baris):
        baris_asli = baris[i]
        line = baris_asli.strip()

        # Komentar LaTeX (% ...) tidak punya padanan di mode Word — dibuang.
        # Juga sisa round-trip: komentar preamble yang sempat bocor ke body lalu
        # di-escape jadi "\% ..." (tampil sebagai teks "% Margin template..." di
        # editor Word dan merusak sampul).
        if line.startswith("%") or line.startswith(r"\%"):
            i += 1
            continue

        # Verbatim/lstlisting: isinya disalin apa adanya, termasuk indentasi.
        if re.match(r"^\\begin\{(verbatim|lstlisting)\}", line):
            penutup = re.compile(r"^\\end\{(verbatim|lstlisting)\}")
            out.append("```")
            i += 1
            while i < len(baris) and not penutup.match(baris[i].strip()):
                out.append(baris[i])
                i += 1
            out.append("```")
            i += 1
            continue

        # Tabular: sel dikumpulkan dulu, dirender saat lingkungannya ditutup.
        if line.startswith(r"\begin{tabular}"):
            tabel_buffer = []
            i += 1
            continue
        if tabel_buffer is not None:
            if line.startswith(r"\end{tabular}"):
                out.append(_tabular_ke_markdown(tabel_buffer))
                tabel_buffer = None
            elif line and not line.startswith("\\"):
                tabel_buffer.append(line)
            i += 1
            continue

        # Blok tengah (sampul/pengesahan): tandai <center> … </center> supaya
        # mode Word dan ekspor DOCX tahu perataannya, bukan dicetak mentah.
        if line.startswith(r"\begin{center}"):
            out.append("<center>")
            i += 1
            continue
        if line.startswith(r"\end{center}"):
            out.append("</center>")
            i += 1
            continue

        # Pembungkus tabel & gambar tidak membawa isi.
        if line.startswith((r"\begin{table}", r"\end{table}")):
            i += 1
            continue

        if line.startswith(tuple(_ABAIKAN_AWALAN)):
            i += 1
            continue

        m = _HEADING_TEX.match(line)
        if m:
            tingkat = _TINGKAT_HEADING[m.group(1)]
            out.append(f"{'#' * tingkat} {_inline_ke_markdown(m.group(2))}")
            i += 1
            continue

        m = _GAMBAR_TEX.search(line)
        if m:
            # Caption biasanya di baris berikutnya di dalam figure.
            alt = ""
            if i + 1 < len(baris):
                cap = _CAPTION_TEX.search(baris[i + 1])
                if cap:
                    alt = _inline_ke_markdown(cap.group(1))
                    i += 1
            out.append(f"![{alt}]({m.group(1)})")
            i += 1
            continue

        # Caption yatim (mis. tabel) tetap berguna sebagai teks.
        cap = _CAPTION_TEX.match(line)
        if cap:
            out.append(_inline_ke_markdown(cap.group(1)))
            i += 1
            continue

        if line.startswith(r"\begin{itemize}"):
            i += 1
            continue
        if line.startswith(r"\begin{enumerate}"):
            nomor_enumerate = 1
            i += 1
            continue
        if line.startswith((r"\end{itemize}", r"\end{enumerate}")):
            nomor_enumerate = 0
            i += 1
            continue
        if line.startswith(r"\item"):
            isi = _inline_ke_markdown(re.sub(r"^\\item\s*", "", line))
            if nomor_enumerate:
                out.append(f"{nomor_enumerate}. {isi}")
                nomor_enumerate += 1
            else:
                out.append(f"- {isi}")
            i += 1
            continue

        # Baris daftar isi hasil _tabel_toc_ke_latex.
        m = _DOTFILL_TEX.match(line)
        if m:
            judul = _inline_ke_markdown(m.group(1))
            halaman = _inline_ke_markdown(m.group(2))
            out.append(f"{judul} — {halaman}" if halaman else judul)
            i += 1
            continue

        if not line:
            out.append("")
            i += 1
            continue

        teks = _inline_ke_markdown(line)
        if teks:
            out.append(teks)
        i += 1

    # Rapatkan baris kosong beruntun jadi satu pemisah paragraf.
    hasil: list[str] = []
    for b in out:
        if b == "" and hasil and hasil[-1] == "":
            continue
        hasil.append(b)
    return "\n".join(hasil).strip() + "\n"


def markdown_to_latex(markdown_text: str, *, preserve_source: bool = False) -> str:
    """Markdown → source LaTeX lengkap (preamble + body + postamble)."""
    lines = markdown_text.split("\n")
    out: list[str] = [_PREAMBLE]
    fig_count = 0
    in_center = False
    # Blok center PERTAMA dokumen adalah sampul dari impor PDF → ditutup
    # dengan ganti halaman supaya pengesahan tidak ikut naik ke dasar sampul.
    # Blok center lain (mis. `<center>` di tengah berkas .md impor) tidak
    # boleh memaksa halaman baru di tengah dokumen.
    isi_awal = True
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        # Penanda halaman baru dari impor PDF: batas halaman muka/bab tidak
        # bisa diturunkan LaTeX dari teks, jadi importer menandainya eksplisit.
        # Penanda tepat setelah sampul dilewati — `</center>` sampul sudah
        # memberi `\newpage`; menambah lagi membuat halaman kosong.
        if stripped == "<newpage>":
            if not (out and out[-1] == r"\newpage"):
                out.append(r"\newpage")
            i += 1
            continue

        # Penanda halaman sampul dari impor PDF: isi dibungkus blok tengah.
        if stripped == "<center>":
            in_center = True
            if isi_awal:
                # Sampul halaman pertama: matikan nomor halaman di halaman ini
                # (PDF asli sampul tidak bernomor). Hanya blok center pembuka
                # yang mendapat \\thispagestyle{empty} — blok `<center>` di
                # tengah dokumen (dari berkas .md) tetap bernomor normal.
                out.extend([r"\begin{center}", r"\thispagestyle{empty}"])
            else:
                out.append(r"\begin{center}")
            i += 1
            continue
        if stripped == "</center>":
            in_center = False
            if isi_awal:
                out.extend([r"\end{center}", r"\newpage"])
                # Sampul hanya blok center PERTAMA; blok center berikutnya
                # (heading muka, baris tanda tangan) bukan sampul lagi — kalau
                # tidak, LEMBAR PENGESAHAN ikut dicetak \textbf{\large} dan
                # memaksa halaman baru dua kali.
                isi_awal = False
            else:
                out.append(r"\end{center}")
            i += 1
            continue
        # Konten nyata DI LUAR blok center (sebelum/sesudahnya) menandakan blok
        # center ini bukan sampul pembuka. Isi di dalam blok center tidak
        # menghapus penanda — sampul justru berisi banyak baris.
        if not in_center and stripped and stripped not in (
            "<center>",
            "</center>",
            "<newpage>",
        ):
            isi_awal = False

        # Baris blok tengah dari impor PDF yang seluruhnya tebal (`**JUDUL**`).
        # Impor tidak lagi menjadikannya heading `##` — judul sampul dan judul
        # dokumen yang terulang di lembar pengesahan bukan struktur dan dulu
        # mencemari daftar isi — jadi ukuran cetaknya diberikan di sini. Baris itu
        # berasal dari teks ≥14 pt di PDF asli, dan sebelumnya tercetak sebagai
        # `\textbf{\large}` (sampul) atau `\subsection*` (pengesahan) yang
        # keduanya ~14 pt; `\large` menjaga tingginya tetap sama.
        if in_center:
            m = re.fullmatch(r"\*\*(.+?)\*\*", stripped)
            if m:
                out.append(f"\\textbf{{\\large {_inline(m.group(1))}}}")
                i += 1
                continue

        # Heading
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            if in_center:
                if isi_awal:
                    # Sampul: judul dicetak tebal di tengah, bukan heading bab
                    # (\\subsection* default rata kiri dan ikut penomoran bagian).
                    # Ukuran mengikuti aslinya: baris sampul PDF umumnya 14 pt,
                    # jadi level 2 memakai \\large (14.4 pt) — \\Large (17 pt)
                    # akan membuat sampul lebih tinggi dan meluber ke halaman
                    # berikutnya.
                    ukuran = r"\LARGE" if level == 1 else r"\large"
                    out.append(f"\\textbf{{{ukuran} {_inline(m.group(2))}}}")
                else:
                    # Heading tengah di halaman muka (LEMBAR PENGESAHAN, judul
                    # bab): tetap heading berstruktur (masuk outline) tapi
                    # dicetak di tengah — `\subsection*` di dalam
                    # `\begin{center}` menghasilkan teks tengah.
                    star = "*" if preserve_source else ""
                    out.append(f"\\subsection{star}{{{_inline(m.group(2))}}}")
            else:
                cmd = {1: "section", 2: "subsection", 3: "subsubsection", 4: "paragraph"}[level]
                star = "*" if preserve_source else ""
                out.append(f"\\{cmd}{star}{{{_inline(m.group(2))}}}")
            i += 1
            continue

        # Gambar markdown ![alt](url)
        m = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", stripped)
        if m:
            src = m.group(2)
            # WMF/EMF tidak didukung LaTeX → skip (tampilkan catatan)
            if re.search(r"\.wmf|\.emf", src, re.I):
                out.append(r"\textit{[Gambar WMF tidak didukung LaTeX - lampirkan manual]}%")
                i += 1
                continue
            # Petunjuk skala dari impor PDF: ![Gambar](url?w=0.51). Query dibuang
            # dari path. Nilai dari impor selalu 0.05–0.98; guard rentang membuat
            # URL tulisan tangan seperti `img.png?w=800` tidak ikut di-strip.
            # Tanpa petunjuk (tulisan tangan / impor DOCX yang tidak membawa
            # lebar tampilan aslinya) dipakai default 0.8.
            lebar = 0.8
            cari = re.search(r"\?w=(\d+(?:\.\d+)?)", src)
            if cari and 0 < float(cari.group(1)) <= 1.5:
                lebar = min(1.0, max(0.05, float(cari.group(1))))
                src = re.sub(r"\?w=[\d.]+", "", src)
            fig_count += 1
            alt = m.group(1) or f"Gambar {fig_count}"
            if in_center:
                out.append(f"\\includegraphics[width={lebar:g}\\textwidth]{{{src}}}")
            elif preserve_source:
                out.append(r"\begin{center}")
                out.append(f"\\includegraphics[width={lebar:g}\\textwidth]{{{src}}}")
                out.append(r"\end{center}")
            else:
                out.append(r"\begin{figure}[h]")
                out.append(r"\centering")
                out.append(f"\\includegraphics[width={lebar:g}\\textwidth]{{{src}}}")
                out.append(f"\\caption{{{_escape(alt)}}}")
                out.append(r"\end{figure}")
            i += 1
            continue

        # Tabel
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s\-:|]+\|?$", lines[i + 1].strip()):
            tbl_lines = [stripped]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl_lines.append(lines[i].strip())
                i += 1
            toc = _tabel_toc_ke_latex(tbl_lines)
            out.append(toc if toc is not None else _table_to_latex(tbl_lines))
            continue

        # List
        if re.match(r"^[-*]\s+", stripped):
            out.append(r"\begin{itemize}")
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                item_text = re.sub(r"^[-*]\s+", "", lines[i].strip())
                out.append(f"\\item {_inline(item_text)}")
                i += 1
            out.append(r"\end{itemize}")
            continue
        if re.match(r"^\d+\.\s+", stripped):
            out.append(r"\begin{enumerate}")
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i].strip()):
                item_text = re.sub(r"^\d+\.\s+", "", lines[i].strip())
                out.append(f"\\item {_inline(item_text)}")
                i += 1
            out.append(r"\end{enumerate}")
            continue

        # Kode blok
        if stripped.startswith("```"):
            out.append(r"\begin{verbatim}")
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                out.append(_escape(lines[i]))
                i += 1
            out.append(r"\end{verbatim}")
            i += 1
            continue

        # Paragraf kosong
        if not stripped:
            i += 1
            continue

        # Nomor halaman yang terisolasi dari impor PDF ("vi", "12") — dibuang
        # agar tidak tercetak nyasar di tengah naskah.
        if not preserve_source and _HALAMAN_ONLY.match(stripped):
            i += 1
            continue

        # Listing kode lampiran yang disatukan pengekstrak PDF → verbatim.
        # Dikerjakan setelah penanganan daftar (baris "- " / "1. ") supaya
        # butir daftar biasa yang panjang tidak ikut terjerat.
        if not preserve_source and _deteksi_listing_kode(line):
            out.append(r"\begin{verbatim}")
            out.extend(_pecah_baris_kode(line))
            out.append(r"\end{verbatim}")
            i += 1
            continue

        # Paragraf biasa
        out.append(_inline(stripped))
        out.append("")
        i += 1

    out.append(_POSTAMBLE)
    return "\n".join(out)


# Penanda Markdown yang tidak pernah muncul di LaTeX yang benar: heading pagar
# di awal baris, dan tebal/miring bergaya bintang.
_MARKDOWN_SISA = re.compile(r"^#{1,6}\s+\S|\*\*\S|^[-*]\s+\S", re.M)


def pastikan_latex(teks: str) -> str:
    """Jaring pengaman: balasan model yang masih Markdown dikonversi ke LaTeX.

    Prompt sudah meminta LaTeX, tapi model tidak selalu menurut — dan Markdown
    yang lolos ke draf tercetak mentah ("**tebal**", "# Judul") di PDF. Deteksi
    memakai penanda yang mustahil ada di LaTeX yang benar; bila ditemukan,
    seluruh teks dikonversi lalu preamble-nya dibuang karena hasil ini
    menempel ke draf yang sudah punya preamble sendiri.
    """
    isi = (teks or "").strip()
    if not isi or not _MARKDOWN_SISA.search(isi):
        return isi
    penuh = markdown_to_latex(isi)
    mulai = penuh.find(r"\begin{document}")
    if mulai != -1:
        penuh = penuh[mulai + len(r"\begin{document}") :]
    akhir = penuh.rfind(r"\end{document}")
    if akhir != -1:
        penuh = penuh[:akhir]
    return penuh.strip()


# `\input{bab/01-x}` atau `\include{bab/01-x.tex}` yang berdiri sendiri di
# barisnya. Sengaja tidak mencocokkan yang di tengah baris: `\input` di dalam
# argumen makro lain adalah pola yang jarang dan penggantiannya berisiko.
_INPUT_TEX = re.compile(r"^[ \t]*\\(?:input|include)\{([^}]+)\}[ \t]*$", re.M)
# Batas rekursi. Dua berkas yang saling meng-`\input` akan menggantung tectonic
# selamanya; di sini cukup berhenti dan biarkan barisnya apa adanya supaya
# galatnya muncul di log kompilasi — bukan sebagai proses yang tidak selesai.
_INPUT_KEDALAMAN_MAKS = 5


def _cocokkan_berkas(nama: str, berkas: dict[str, str]) -> str | None:
    """Cari isi berkas untuk argumen `\\input{...}`.

    LaTeX membolehkan `\\input{bab/01-x}` tanpa ekstensi, dan proyek yang
    dipecah menyimpan jalur lengkap `bab/01-x.tex`. Nama dasar juga dicoba
    supaya `\\input{01-x}` dari draf yang ditulis tangan tetap ketemu.
    """
    kandidat = nama.strip().replace("\\", "/").lstrip("./")
    for coba in (kandidat, f"{kandidat}.tex"):
        if coba in berkas:
            return berkas[coba]
    # Cocokkan lewat nama dasar; ambigu bila dua folder punya nama sama, jadi
    # hanya dipakai kalau persis satu berkas yang cocok.
    dasar = kandidat.rsplit("/", 1)[-1]
    cocok = [
        isi
        for jalur, isi in berkas.items()
        if jalur.rsplit("/", 1)[-1] in (dasar, f"{dasar}.tex")
    ]
    return cocok[0] if len(cocok) == 1 else None


def datarkan_input(sumber: str, berkas: dict[str, str], _kedalaman: int = 0) -> str:
    """Ganti `\\input{...}`/`\\include{...}` dengan isi berkasnya.

    Dipakai sebelum kompilasi dan sebelum analisis AI: keduanya perlu melihat
    naskah utuh, bukan `main.tex` yang isinya preamble + daftar `\\input`.
    Berkas yang tidak ditemukan dibiarkan apa adanya — tectonic yang melaporkan
    berkas hilang, dan pesan itu lebih berguna daripada baris yang lenyap diam-diam.
    """
    if not sumber or not berkas or _kedalaman >= _INPUT_KEDALAMAN_MAKS:
        return sumber

    def _ganti(m: re.Match[str]) -> str:
        isi = _cocokkan_berkas(m.group(1), berkas)
        if isi is None:
            return m.group(0)
        return datarkan_input(isi, berkas, _kedalaman + 1)

    return _INPUT_TEX.sub(_ganti, sumber)


def localize_latex_image_paths(
    latex_source: str,
    asset_dirs: list[str] | tuple[str, ...] = (),
) -> str:
    """Resolve uploaded and relative LaTeX images before export rendering."""
    roots = [os.path.abspath(root) for root in asset_dirs if root]

    def resolve(value: str) -> str:
        parsed = urlparse(value)
        path = unquote(parsed.path if parsed.scheme else value)
        # UUID ber-dash (36) maupun tanpa dash (32) — folder di disk memakai
        # bentuk ber-dash, jadi yang tanpa dash dinormalkan dulu.
        match = re.search(r"/uploads/([0-9a-f-]{32,36})/images/(.+)$", path, re.I)
        if match:
            ident = match.group(1)
            if len(ident) == 32:
                ident = f"{ident[:8]}-{ident[8:12]}-{ident[12:16]}-{ident[16:20]}-{ident[20:]}"
            candidate = os.path.abspath(
                os.path.join("uploads", ident, "images", match.group(2))
            )
            if os.path.isfile(candidate):
                return candidate.replace("\\", "/")
        if parsed.scheme in {"http", "https"}:
            return value
        cleaned = unquote(value).replace("\\", "/").lstrip("./")
        candidates = []
        if os.path.isabs(value):
            candidates.append(value)
        for root in roots:
            candidates.extend(
                [os.path.join(root, cleaned), os.path.join(root, os.path.basename(cleaned))]
            )
        for candidate in candidates:
            if os.path.isfile(candidate):
                return os.path.abspath(candidate).replace("\\", "/")
        return value

    return re.sub(
        r"\\includegraphics(\[[^\]]*\])?\{([^}]+)\}",
        lambda match: f"\\includegraphics{match.group(1) or ''}{{{resolve(match.group(2))}}}",
        latex_source or "",
    )


def compile_latex_pdf(
    latex_source: str,
    output_dir: str,
    jobname: str = "laporan",
    asset_dirs: list[str] | tuple[str, ...] = (),
) -> str:
    """Compile source LaTeX → PDF via tectonic. Return path PDF.

    Gambar direferensikan lewat URL absolut; tectonic tidak bisa fetch http,
    jadi URL di-rewrite ke path lokal bila file ada di uploads/.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Rewrite URL gambar → path lokal absolut (bila ada di uploads/)
    # Ekstraksi PDF sering menyimpan ligatur sebagai karakter Unicode terpisah.
    # Font Latin Modern T1 tidak memilikinya, tetapi pasangan huruf biasa setara.
    latex_source = latex_source.replace("\ufb01", "fi").replace("\ufb02", "fl")

    tex_source = localize_latex_image_paths(latex_source, asset_dirs)

    tex_path = os.path.join(output_dir, f"{jobname}.tex")
    with open(tex_path, "w", encoding="utf-8") as fh:
        fh.write(tex_source)
    try:
        env = os.environ.copy()
        # Tectonic butuh config fontconfig; fallback ke bawaan.
        env.setdefault("FONTCONFIG_FILE", "")
        result = subprocess.run(
            [
                _TECTONIC,
                tex_path,
                "--outdir",
                output_dir,
                "-Z",
                "continue-on-errors",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TECTONIC_TIMEOUT_SECONDS,
            env=env,
        )
        if result.returncode != 0:
            # Kode keluar disertakan: saat memori habis, tectonic mati tanpa
            # menulis apa pun ke stdout/stderr, dan pesan "gagal tanpa keluaran
            # log" saja membuatnya tampak seperti cacat kode padahal penyebabnya
            # lingkungan. Kode keluar (mis. 0xC0000005/-1073741819) yang
            # membedakannya dari galat LaTeX biasa.
            log = (
                result.stderr
                or result.stdout
                or f"Tectonic berhenti dengan kode {result.returncode} tanpa keluaran log "
                "— biasanya memori sistem habis, bukan galat LaTeX."
            )
            raise RuntimeError(f"Tectonic gagal (kode {result.returncode}):\n{log[-1500:]}")
    except subprocess.TimeoutExpired as exc:
        # Tanpa penangkapan ini exception menerobos FastAPI sebagai HTTP 500
        # tanpa isi, padahal penyebabnya adalah dokumen yang besar/berat.
        raise RuntimeError(
            f"Kompilasi LaTeX melewati batas waktu "
            f"{_TECTONIC_TIMEOUT_SECONDS} detik. Dokumen terlalu besar atau "
            "terdapat struktur yang membuat tectonic berhenti; periksa bagian "
            "akhir log berikut, lalu coba lagi."
        ) from exc
    except FileNotFoundError:
        raise RuntimeError("tectonic.exe tidak ditemukan.")
    pdf_path = os.path.join(output_dir, f"{jobname}.pdf")
    if not os.path.exists(pdf_path):
        raise RuntimeError("PDF tidak dihasilkan tectonic.")
    return pdf_path
