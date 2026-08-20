"""Typeset renderer — tampilan dokumen rapi ala LaTeX (PRD v2.5 §8).

Mengubah markdown → HTML dengan kaidah tipografi ilmiah:
- Penomoran bab/sub-bab otomatis (1, 1.1, 1.1.1)
- Penomoran gambar/tabel otomatis dengan caption (Gambar 1, Tabel 1)
- Format sitasi [n] & daftar pustaka sesuai gaya aktif
- CSS A4: margin konsisten, header/footer, nomor halaman (via xhtml2pdf)
"""

from __future__ import annotations

import re

import markdown2

_TYPESET_CSS = """
@page {
  size: A4;
  margin: 2.2cm 2cm 2cm 2cm;
}
body { font-family: 'Times New Roman', Georgia, serif; font-size: 12pt; line-height: 1.6; color: #111; text-align: justify; }
h1, h2, h3, h4 { font-family: 'Times New Roman', serif; color: #000; text-align: left; }
h1 { font-size: 16pt; margin: 0.9em 0 0.4em; }
h2 { font-size: 14pt; margin: 0.8em 0 0.3em; }
h3 { font-size: 12.5pt; margin: 0.6em 0 0.2em; }
table { border-collapse: collapse; width: 100%; margin: 0.6em auto; caption-side: top; }
td, th { border: 1px solid #888; padding: 4px 8px; font-size: 11pt; }
th { background: #f2f2f2; }
img { max-width: 100%; display: block; margin: 0.5em auto; }
figure { margin: 0.8em 0; }
.caption { text-align: center; font-size: 10.5pt; font-style: italic; margin: 0.2em 0 0.8em; }
blockquote { border-left: 3px solid #bbb; margin: 0.6em 0; padding-left: 1em; color: #444; }
pre, code { font-family: 'Courier New', monospace; font-size: 10pt; }
pre { background: #f7f7f7; padding: 8px; border: 1px solid #ddd; }
/* Daftar isi/tabel/gambar: judul kiri, nomor halaman kanan, titik pengisi
   di antaranya. Tidak boleh ikut perataan justify badan teks.
   Di layar dipakai flex (judul menyusut mengikuti isinya, titik pengisi
   menyerap sisa ruang). Markupnya tetap <table> karena ekspor PDF memakai
   xhtml2pdf yang tidak mengenal flexbox — aturan cetak di bawah
   mengembalikannya jadi tabel dengan lebar kolom yang dipatok. */
.toc { margin: 0.3em 0 1em; width: 100%; border-collapse: collapse; }
.toc, .toc > tbody { display: block; }
.toc-entry { display: flex; align-items: flex-end; }
.toc-entry > td { display: block; text-align: left; line-height: 1.75; border: none; padding: 0; font-size: 12pt; }
.toc-entry .toc-title { flex: 0 1 auto; padding-right: 0.4em; }
.toc-entry .toc-dots { flex: 1 1 auto; min-width: 1.2em; margin-bottom: 0.32em; border-bottom: 1px dotted #999; }
.toc-entry .toc-page { flex: 0 0 auto; white-space: nowrap; text-align: right; padding-left: 0.4em; }
.toc-entry.lvl-1 .toc-title { padding-left: 1.4em; }
.toc-entry.lvl-2 .toc-title { padding-left: 2.8em; }
/* xhtml2pdf (ekspor PDF) tidak mengenal flexbox dan tidak bisa menghitung
   lebar sel otomatis — tanpa lebar tetap ia gagal "negative availWidth". */
@media print {
  .toc, .toc > tbody { display: table; width: 100%; }
  .toc-entry { display: table-row; }
  .toc-entry > td { display: table-cell; vertical-align: bottom; }
  .toc-entry .toc-title { width: 80%; }
  .toc-entry .toc-dots { width: 12%; }
  .toc-entry .toc-page { width: 8%; }
  /* Tabel data berkolom banyak: xhtml2pdf membagi lebar rata, sehingga padding
     bisa menghabiskan seluruh lebar sel dan menggagalkan render
     ("negative availWidth"). Padding dipangkas agar selalu tersisa ruang. */
  td, th { padding: 1px 2px; font-size: 9.5pt; }
  .toc-entry > td { padding: 0; font-size: 12pt; }
}
/* Blok tanda tangan/pengesahan: tiap baris berdiri sendiri, tidak dijustify
   dan tidak digabung jadi satu paragraf. */
.form-block { margin: 0.6em 0 1.2em; text-align: left; }
.form-block .form-line { display: block; line-height: 1.8; }
"""

# Ditambahkan hanya saat mencetak lewat Chromium. Membatalkan tambalan
# @media print di atas (yang ada semata demi keterbatasan xhtml2pdf) lalu
# menerapkan aturan cetak yang sesungguhnya: spasi 1,5 dan margin mengikuti
# template kampus, tabel/gambar tidak terpotong antar halaman, dan setiap bab
# mulai di halaman baru.
_CSS_CETAK_CHROMIUM = """
/* Margin diatur oleh pemanggil (chromium_pdf.MARGIN) agar nomor halaman punya
   ruang di bawah. Bila @page juga memberi margin, keduanya bertumpuk. */
@page { size: A4; margin: 0; }
body { line-height: 1.5; }
@media print {
  /* Daftar isi kembali memakai flexbox: titik pengisi menyerap sisa ruang
     sehingga nomor halaman selalu rapat di tepi kanan. */
  .toc, .toc > tbody { display: block; }
  .toc-entry { display: flex; align-items: flex-end; }
  .toc-entry > td { display: block; padding: 0; font-size: 12pt; }
  .toc-entry .toc-title { width: auto; flex: 0 1 auto; }
  .toc-entry .toc-dots { width: auto; flex: 1 1 auto; }
  .toc-entry .toc-page { width: auto; flex: 0 0 auto; }
  /* Padding tabel kembali normal; Chromium menghitung lebar kolom sendiri. */
  td, th { padding: 4px 8px; font-size: 11pt; }
  /* Jangan sampai baris tabel, gambar, atau keterangannya terbelah halaman. */
  tr, img, figure, .caption { break-inside: avoid; page-break-inside: avoid; }
  table { break-inside: auto; }
  thead { display: table-header-group; }
  /* Judul tidak boleh menggantung sendiri di dasar halaman. */
  h1, h2, h3, h4 { break-after: avoid; page-break-after: avoid; }
  h1 { break-before: page; page-break-before: always; }
  h1:first-of-type { break-before: auto; page-break-before: auto; }
  p { orphans: 2; widows: 2; }
}
"""

# Judul bagian awal (front matter) yang menurut kaidah penulisan ilmiah tidak
# ikut bernomor bab.
_FRONT_MATTER = (
    "daftar isi", "daftar tabel", "daftar gambar", "daftar notasi",
    "daftar lampiran", "daftar singkatan", "daftar pustaka", "kata pengantar",
    "abstrak", "abstract", "lembar pengesahan", "surat pernyataan",
    "halaman persembahan", "pedoman penggunaan", "motto", "riwayat hidup",
    "ucapan terima kasih",
)

# Baris daftar isi: "Judul . . . . . 12" atau "Judul ......... 12".
_LEADER = re.compile(r"^(?P<judul>.*?\S)\s*(?:\.\s*){4,}\s*(?P<hal>[ivxlcdm\d]*)\s*$", re.I)
# Baris yang isinya hanya titik pengisi — kelanjutan judul di baris sebelumnya.
_DOTS_ONLY = re.compile(r"^(?:\.\s*){2,}$")
# Titik pengisi beserta nomor halaman, sedangkan judulnya di baris sebelumnya:
#   "DAFTAR NOTASI" / ". . . . . . . xvi"
_DOTS_PAGE = re.compile(r"^(?:\.\s*){2,}\s*(?P<hal>[ivxlcdm]{1,7}|\d{1,4})\s*$", re.I)
# Label yang berdiri sendiri sebelum baris daftar isi, mis. "Bab 1" atau "3.2".
_TOC_LABEL = re.compile(r"^(?:bab\s+[ivxlcdm\d]+|lampiran\s+\w|\d+(?:\.\d+)*)$", re.I)
# Nomor halaman yang terlempar ke barisnya sendiri.
_PAGE_ONLY = re.compile(r"^(?:[ivxlcdm]{1,7}|\d{1,4})$", re.I)
# Baris tanda tangan/isian formulir ("NIDN. ......", "Tanggal ....."). Titik
# pengisinya mirip daftar isi, tetapi tidak berujung nomor halaman sehingga
# tidak boleh diubah jadi entri daftar isi.
_ISIAN = re.compile(
    r"^(?:nidn|nip|npm|nim|tanggal|tgl|hari|di\s+bandung|bandung|nama|"
    r"jabatan|mengetahui|menyetujui|pada\s+sidang|oleh|disusun|diajukan|"
    r"program\s+studi|jurusan|fakultas|politeknik|universitas)\b",
    re.I,
)
# Ruang isian dalam tanda kurung: "(..........................)".
_KURUNG_ISIAN = re.compile(r"^\(\s*\.{3,}\s*\)$")
# Label jabatan penanda tangan.
_JABATAN = re.compile(
    r"^(?:pembimbing|penguji|ketua|dekan|kepala|direktur|wakil|dosen)\b", re.I)
# Nama beserta gelar akademik, mis. "Widia Resdiana, S.S., M.Pd.".
_BERGELAR = re.compile(r",\s*(?:S|M|A|Dr|Ir|Prof)\.", re.I)


def _looks_like_toc_entry(judul: str, hal: str) -> bool:
    """Benarkah ini entri daftar isi, bukan baris isian tanda tangan?"""
    if _ISIAN.match(judul.strip()):
        return False
    # Entri daftar isi selalu berujung nomor halaman.
    return bool(hal)


def _tanpa_penekanan(teks: str) -> str:
    """Buang penanda tebal/miring markdown dari judul entri daftar isi.

    Entri daftar isi tidak dilewatkan markdown2 (ia sudah berupa HTML), jadi
    penanda seperti `_Current System_` akan tercetak apa adanya bila dibiarkan.
    """
    teks = re.sub(r"\*\*(.+?)\*\*", r"\1", teks)
    teks = re.sub(r"(?<![A-Za-z0-9])_(.+?)_(?![A-Za-z0-9])", r"\1", teks)
    return re.sub(r"\*(.+?)\*", r"\1", teks)


def _tambah_entri(entries: list[str], judul: str, hal: str) -> None:
    """Tambahkan satu baris entri daftar isi; indentasi ikut kedalaman nomor."""
    judul = _tanpa_penekanan(judul)
    depth = judul.count(".") if re.match(r"^\d+(\.\d+)+", judul) else 0
    cls = f" lvl-{min(depth, 2)}" if depth else ""
    entries.append(
        f"<tr class='toc-entry{cls}'>"
        f"<td class='toc-title'>{_esc(judul)}</td>"
        f"<td class='toc-dots'></td>"
        f"<td class='toc-page'>{_esc(hal)}</td></tr>"
    )


def _sel_baris_tabel(line: str) -> list[str]:
    """Pecah satu baris tabel pipa markdown jadi daftar sel."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.replace("<br>", " ").strip() for c in s.split("|")]


_PEMISAH_TABEL = re.compile(r"^\|?[\s:|-]*-{2,}[\s:|-]*\|?$")


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
            # Nomor bagian terpotong di tengah: "3." + "1.1" → "3.1.1".
            hasil += c
        elif _TOC_LABEL.match(hasil.strip()) or hasil.endswith((".", " ")):
            hasil = f"{hasil} {c}"
        else:
            hasil += c
    return re.sub(r"\s{2,}", " ", hasil).strip()


def _build_toc_from_tables(markdown_text: str) -> tuple[str, dict[str, str]]:
    """Ubah tabel markdown yang isinya entri daftar isi jadi blok daftar isi.

    Pengekstrak PDF mengenali halaman daftar isi sebagai tabel dua kolom
    (nomor bagian | judul + titik pengisi + halaman). Bila dibiarkan, tabel itu
    tergambar bergaris dan ikut bernomor "Tabel N", padahal ia bukan tabel data.
    """
    lines = markdown_text.splitlines()
    out: list[str] = []
    blocks: dict[str, str] = {}
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("|"):
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].lstrip().startswith("|"):
            j += 1
        blok = lines[i:j]
        isi = [b for b in blok if not _PEMISAH_TABEL.match(b.strip())]
        cocok: list[tuple[str, str]] = []
        for baris in isi:
            teks = _gabung_sel(_sel_baris_tabel(baris))
            m = _LEADER.match(teks)
            if m and m.group("hal").strip() and _looks_like_toc_entry(
                m.group("judul"), m.group("hal")
            ):
                cocok.append((m.group("judul").strip(), m.group("hal").strip()))
        # Ambang 60%: satu-dua baris berpengisi titik di tabel data biasa tidak
        # boleh membuat seluruh tabel diperlakukan sebagai daftar isi.
        if isi and len(cocok) >= max(2, int(0.6 * len(isi))):
            entries: list[str] = []
            for judul, hal in cocok:
                _tambah_entri(entries, judul, hal)
            key = f"TOCTBL{len(blocks)}ENDTOC"
            blocks[key] = (
                f"<table class='toc'><tbody>{''.join(entries)}</tbody></table>"
            )
            out.extend(["", key, ""])
        else:
            out.extend(blok)
        i = j
    return "\n".join(out), blocks


def _build_toc_blocks(markdown_text: str) -> tuple[str, dict[str, str]]:
    """Ubah baris berpengisi titik jadi blok HTML daftar isi.

    Dikerjakan sebelum markdown2 karena markdown2 menyatukan baris-baris itu
    jadi satu paragraf justify sehingga titik pengisinya berantakan. Blok hasil
    disimpan sebagai placeholder agar tidak ikut diproses markdown2.
    """
    lines = markdown_text.splitlines()
    out: list[str] = []
    blocks: dict[str, str] = {}
    entries: list[str] = []
    i = 0

    def flush() -> None:
        if not entries:
            return
        key = f"TOCBLOCK{len(blocks)}ENDTOC"
        blocks[key] = f"<table class='toc'><tbody>{''.join(entries)}</tbody></table>"
        out.append("")
        out.append(key)
        out.append("")
        entries.clear()

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        # Label ("Bab 1", "1.4") yang terpisah dari judulnya di baris berikutnya.
        # Judul itu bisa berbentuk lengkap ("Judul . . . 4") maupun terpecah
        # ("Judul" / ". . . 4" / "4"), jadi ketiga bentuk ikut diperiksa.
        prefix = ""
        if _TOC_LABEL.match(stripped) and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            berlanjut = (
                _LEADER.match(nxt)
                or (i + 2 < len(lines) and _DOTS_PAGE.match(lines[i + 2].strip()))
                or (
                    i + 3 < len(lines)
                    and _DOTS_ONLY.match(lines[i + 2].strip())
                    and _PAGE_ONLY.match(lines[i + 3].strip())
                )
            )
            if nxt and not _DOTS_ONLY.match(nxt) and berlanjut:
                prefix = stripped + " "
                i += 1
                stripped = nxt

        m = _LEADER.match(stripped)
        if not m or not m.group("judul").strip(" ."):
            # (a) Judulnya di baris ini, titik pengisi + nomor halaman di baris
            #     berikutnya:  "DAFTAR NOTASI" / ". . . . . xvi"
            if (
                stripped
                and not _DOTS_ONLY.match(stripped)
                and i + 1 < len(lines)
                and _DOTS_PAGE.match(lines[i + 1].strip())
                and not _ISIAN.match(stripped)
            ):
                judul = (prefix + stripped).strip()
                _tambah_entri(entries, judul,
                              _DOTS_PAGE.match(lines[i + 1].strip()).group("hal"))
                i += 2
                continue
            # (b) Judul / titik pengisi / nomor halaman terpecah tiga baris:
            #     "2.11 Landasan Perangkat Lunak" / ". . . . ." / "15"
            if (
                stripped
                and not _DOTS_ONLY.match(stripped)
                and i + 2 < len(lines)
                and _DOTS_ONLY.match(lines[i + 1].strip())
                and _PAGE_ONLY.match(lines[i + 2].strip())
                and not _ISIAN.match(stripped)
            ):
                judul = (prefix + stripped).strip()
                _tambah_entri(entries, judul, lines[i + 2].strip())
                i += 3
                continue
            flush()
            out.append(raw)
            i += 1
            continue

        judul = (prefix + m.group("judul")).strip()
        hal = m.group("hal").strip()
        # Nomor bagian kadang tercetak SESUDAH judulnya, lalu nomor halaman:
        #   "Analisis Sistem Berjalan . . ." / "3.1.1" / "17"
        if (
            not hal
            and not prefix
            and i + 2 < len(lines)
            and _TOC_LABEL.match(lines[i + 1].strip())
            and _PAGE_ONLY.match(lines[i + 2].strip())
        ):
            judul = f"{lines[i + 1].strip()} {judul}"
            hal = lines[i + 2].strip()
            i += 2
        # Nomor halaman kerap terdorong ke baris sesudahnya oleh ekstraktor PDF.
        elif not hal and i + 1 < len(lines) and _PAGE_ONLY.match(lines[i + 1].strip()):
            hal = lines[i + 1].strip()
            i += 1

        if not _looks_like_toc_entry(judul, hal):
            flush()
            out.append(raw)
            i += 1
            continue

        _tambah_entri(entries, judul, hal)
        i += 1

    flush()
    return "\n".join(out), blocks


def _baris_formulir(line: str) -> bool:
    """Baris bagian tanda tangan/pengesahan yang jeda barisnya harus dijaga."""
    s = line.strip()
    if not s or len(s) > 80 or s.startswith("#") or s.startswith(("-", "*", "|")):
        return False
    if _ISIAN.match(s) or _KURUNG_ISIAN.match(s) or _JABATAN.match(s):
        return True
    if _BERGELAR.search(s) and s.endswith("."):
        return True
    # "NIDN. ......" sudah tercakup _ISIAN; ini menangkap isian titik lainnya
    # yang tidak berujung nomor halaman (jadi bukan entri daftar isi).
    m = _LEADER.match(s)
    return bool(m and not m.group("hal").strip())


def _jangkar_formulir(line: str) -> bool:
    """Penanda pasti blok formulir — supaya prosa biasa tidak ikut terjaring."""
    s = line.strip()
    return bool(_ISIAN.match(s) or _KURUNG_ISIAN.match(s))


def _build_form_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Jaga jeda baris pada blok tanda tangan/pengesahan.

    Naskah hasil ekstraksi PDF terpotong satu baris per baris tampilan, dan
    markdown2 menyatukan baris-baris itu jadi satu paragraf justify. Untuk
    prosa hal itu benar, tetapi halaman pengesahan jadi kalimat beruntun.
    Blok yang terdeteksi disimpan sebagai placeholder agar tidak diolah lagi.
    """
    lines = text.splitlines()
    out: list[str] = []
    blocks: dict[str, str] = {}
    i = 0
    while i < len(lines):
        if not _baris_formulir(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and _baris_formulir(lines[j]):
            j += 1
        run = lines[i:j]
        if len(run) >= 2 and any(_jangkar_formulir(x) for x in run):
            key = f"FORMBLOCK{len(blocks)}ENDFORM"
            isi = "".join(
                f"<span class='form-line'>{_esc(x.strip())}</span>" for x in run
            )
            blocks[key] = f"<div class='form-block'>{isi}</div>"
            out.extend(["", key, ""])
        else:
            out.extend(run)
        i = j
    return "\n".join(out), blocks


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _is_front_matter(title: str) -> bool:
    plain = re.sub(r"<[^>]+>", "", title).strip().lower()
    return any(plain.startswith(fm) for fm in _FRONT_MATTER)


def _judul_bukan_bagian(title: str) -> bool:
    """Heading palsu dari halaman sampul/pengesahan yang tak boleh bernomor.

    Ekstraktor PDF menandai baris berfont besar seperti "Disusun Oleh :",
    "Pembimbing Utama", atau nama bergelar sebagai heading. Baris itu bukan
    sub-bab, jadi penomoran otomatis harus melewatinya.
    """
    plain = re.sub(r"<[^>]+>", "", title).strip()
    if not plain:
        return True
    return bool(
        _ISIAN.match(plain) or _JABATAN.match(plain) or _KURUNG_ISIAN.match(plain)
        or (_BERGELAR.search(plain) and len(plain) <= 80)
    )


def _number_headings(html: str) -> str:
    """Berikan nomor otomatis pada heading h1/h2/h3.

    Halaman sampul, pengesahan, abstrak, daftar isi/tabel/gambar, dan sejenisnya
    tidak diberi nomor. Penomoran baru dimulai setelah "Bab N" (atau heading
    pertama bila laporan tidak memakai penanda bab), sehingga tidak muncul nomor
    semu seperti "0.18." atau "1. LAPORAN TUGAS AKHIR". Judul bab yang terbelah
    impor ("Bab 1" lalu "PENDAHULUAN") disatukan: bagian keduanya tidak
    dinomori sebagai bab baru.
    """
    counters = [0, 0, 0]
    lanjutan_bab = False
    # Laporan yang memakai penanda "Bab N": bagian sebelum bab pertama (sampul,
    # pengesahan, daftar ...) dibiarkan tanpa nomor. Tanpa penanda tersebut,
    # penomoran langsung dimulai dari heading bagian pertama.
    ada_markah_bab = any(
        re.match(r"^\s*(?:bab|chapter)\s+[ivxlcdm\d]+\b",
                 re.sub(r"<[^>]+>", "", m.group(2)).strip(), re.I)
        for m in re.finditer(r"<(h[1-6])>([\s\S]*?)</\1>", html)
    )

    def repl(match: re.Match) -> str:
        nonlocal lanjutan_bab
        tag = match.group(1)
        title = match.group(2)
        level = int(tag[1])  # h1→1, h2→2, h3→3

        plain = re.sub(r"<[^>]+>", "", title).strip()

        # Judul bab "Bab N": majukan penghitung, angkanya tidak ditulis sendiri.
        if re.match(r"^\s*(?:bab|chapter)\s+[ivxlcdm\d]+\b", plain, re.I):
            counters[0] += 1
            counters[1] = counters[2] = 0
            lanjutan_bab = True
            return f"<{tag}>{title}</{tag}>"

        # Kelanjutan judul bab yang terbelah ("Bab 1" / "PENDAHULUAN"): heading
        # berikutnya masih bagian judul bab, bukan bab baru.
        if lanjutan_bab:
            lanjutan_bab = False
            return f"<{tag}>{title}</{tag}>"

        if _is_front_matter(title) or _judul_bukan_bagian(title):
            return f"<{tag}>{title}</{tag}>"

        # Judul sudah membawa nomornya sendiri dari berkas aslinya
        # ("2.6 Typesetting dan Format LaTeX") — jangan dinomor dua kali.
        if re.match(r"^\s*\d+(?:\.\d+)+\s*", plain):
            return f"<{tag}>{title}</{tag}>"

        # Sebelum bab pertama laporan berbab: jangan karang nomor semu pada
        # sampul/pengesahan/abstrak (mis. "1. LAPORAN TUGAS AKHIR").
        if ada_markah_bab and counters[0] == 0:
            return f"<{tag}>{title}</{tag}>"

        if level == 1:
            counters[0] += 1
            counters[1] = counters[2] = 0
            num = f"{counters[0]}"
        elif level == 2 or counters[1] == 0:
            # Heading tingkat 3 yang muncul sebelum ada sub-bab tingkat 2
            # akan menghasilkan nomor semu "1.0.1"; diperlakukan sebagai
            # sub-bab biasa supaya penomorannya tetap rapi.
            counters[1] += 1
            counters[2] = 0
            num = f"{counters[0]}.{counters[1]}"
        else:
            counters[2] += 1
            num = f"{counters[0]}.{counters[1]}.{counters[2]}"

        return f"<{tag}><span class='sec-num'>{num}.</span> {title}</{tag}>"

    return re.sub(r"<(h[123])>([\s\S]*?)</\1>", repl, html)


def _number_figures_tables(html: str) -> str:
    """Beri caption + nomor otomatis untuk gambar & tabel.

    Dijalankan atas keluaran markdown2, jadi yang dicari tag <img>, bukan
    sintaks markdown `![]()` yang saat ini sudah tidak tersisa.
    """
    fig_count = 0
    tbl_count = 0

    def fig_repl(match: re.Match) -> str:
        nonlocal fig_count
        fig_count += 1
        tag = match.group("img")
        alt_m = re.search(r"alt=[\"']([^\"']*)[\"']", tag)
        alt = (alt_m.group(1) if alt_m else "").strip()
        # Alt bawaan hasil impor ("Gambar") bukan keterangan sungguhan.
        judul = f"Gambar {fig_count}. {alt}" if alt and alt.lower() != "gambar" else f"Gambar {fig_count}"
        return f"<figure>{tag}<figcaption class='caption'>{judul}</figcaption></figure>"

    def tbl_repl(match: re.Match) -> str:
        nonlocal tbl_count
        atribut = match.group(1)
        inner = match.group(2)
        # Blok daftar isi juga berupa <table> (demi kompatibilitas xhtml2pdf),
        # tetapi ia bukan tabel data sehingga tidak diberi nomor "Tabel N".
        if "toc" in atribut:
            return match.group(0)
        tbl_count += 1
        return (
            f"<table{atribut}><caption class='caption'>Tabel {tbl_count}</caption>{inner}</table>"
        )

    # <p> pembungkus ikut diserap supaya <figure> tidak bersarang di dalam <p>.
    html = re.sub(r"<p>\s*(?P<img><img\b[^>]*/?>)\s*</p>", fig_repl, html)
    # Gambar sisa (mis. di dalam tabel atau butir daftar) — yang sudah masuk
    # <figure> pada tahap di atas dilewati agar tidak terbungkus dua kali.
    html = re.sub(r"(?<!<figure>)(?P<img><img\b[^>]*/?>)", fig_repl, html)
    html = re.sub(r"<table([^>]*)>(.*?)</table>", tbl_repl, html, flags=re.S)
    # Sel kosong membuat xhtml2pdf menghitung lebar kolomnya nol, lalu gagal
    # dengan "negative availWidth". Diisi spasi keras agar tetap punya lebar.
    html = re.sub(r"<(td|th)([^>]*)>\s*</\1>", r"<\1\2>&nbsp;</\1>", html)
    return html


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


def _percik_code(line: str) -> list[str] | None:
    """Fence listing kode yang disatukan pengekstrak PDF.

    Pengekstrak menyatukan baris kode bernomor jadi satu paragraf panjang.
    Kalau dibiarkan, markdown2 menjadikannya paragraf justify yang berantakan.
    Kembalikan baris-baris kode yang sudah dipisah, atau None bila ini bukan
    listing kode.
    """
    if len(line) < 200 or len(_KODE_PECAH.findall(line)) < 3:
        return None
    if not _KODE_MARK.search(line):
        return None
    return _pulihkan_baris_kode(_KODE_PECAH.split(line.strip()))


def _fence_listing_kode(markdown_text: str) -> str:
    """Bungkus listing kode satu baris hasil impor PDF jadi blok kode bertanda."""
    baris = markdown_text.splitlines()
    keluar: list[str] = []
    for ln in baris:
        pecah = _percik_code(ln)
        if pecah is None:
            keluar.append(ln)
        else:
            keluar.append("```")
            keluar.extend(pecah)
            keluar.append("```")
    return "\n".join(keluar)


def markdown_to_typeset_html(markdown_text: str, *, untuk_chromium: bool = False) -> str:
    """Markdown → HTML typeset rapi (dengan nomor heading/gambar/tabel).

    `untuk_chromium` mematikan tambalan khusus xhtml2pdf. Tambalan itu memangkas
    padding dan ukuran huruf tabel demi menghindari kegagalan "negative
    availWidth", dan mengubah daftar isi dari flexbox jadi tabel berlebar patok.
    Chromium tidak punya batasan tersebut, jadi tanpa dimatikan hasil cetaknya
    justru lebih jelek daripada yang tampil di layar.
    """
    prepared = _fence_listing_kode(markdown_text or "")
    prepared, tbl_toc = _build_toc_from_tables(prepared)
    prepared, toc_blocks = _build_toc_blocks(prepared)
    prepared, form_blocks = _build_form_blocks(prepared)
    body = markdown2.markdown(
        prepared,
        extras=["fenced-code-blocks", "tables", "strike", "task_list"],
    )
    body = _number_headings(body)
    body = _number_figures_tables(body)
    # Nomor halaman yang terisolasi dari impor PDF ("<p>vi</p>") — dibuang
    # agar tidak tercetak nyasar di tengah naskah.
    body = re.sub(r"<p>((?:[ivxlcdm]{1,7}|\d{1,3}))</p>", "", body, flags=re.I)
    for key, block in {**tbl_toc, **toc_blocks, **form_blocks}.items():
        body = body.replace(f"<p>{key}</p>", block).replace(key, block)
    css = _TYPESET_CSS + (_CSS_CETAK_CHROMIUM if untuk_chromium else "")
    return (
        f"<html><head><meta charset='utf-8'><style>{css}</style></head>"
        f"<body>{body}</body></html>"
    )


def typeset_to_pdf(markdown_text: str, output_path: str) -> str:
    """Render typeset → PDF A4 (header/footer/nomor halaman).

    Mesin utama Chromium: hasilnya sama dengan pratinjau di layar, mendukung
    flexbox, lebar kolom otomatis, dan gambar besar. Bila Chromium tidak
    tersedia, jatuh ke xhtml2pdf yang tidak mendukung semua itu tetapi masih
    menghasilkan berkas yang terbaca.
    """
    from app.services import chromium_pdf

    if chromium_pdf.tersedia():
        html = markdown_to_typeset_html(markdown_text, untuk_chromium=True)
        return chromium_pdf.html_to_pdf(html, output_path)

    from io import BytesIO

    from xhtml2pdf import pisa

    from app.services.pdf_exporter import resolve_uri

    html = markdown_to_typeset_html(markdown_text)
    buf = BytesIO()
    # resolve_uri memetakan URL /uploads ke berkas lokal; tanpa itu xhtml2pdf
    # mengambil gambar lewat HTTP ke peladen yang sedang merender PDF ini.
    result = pisa.CreatePDF(html, dest=buf, link_callback=resolve_uri)
    if result.err:
        raise RuntimeError("Gagal render typeset PDF (xhtml2pdf error).")
    with open(output_path, "wb") as fh:
        fh.write(buf.getvalue())
    return output_path
