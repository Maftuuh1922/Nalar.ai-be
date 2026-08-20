"""Sitasi otomatis oleh agen: `[n]` dari server, Daftar Pustaka dari naskah.

Cacat yang diuji di sini terukur pada laporan nyata pengguna: agen berhasil
menulis Bab 1–5 lengkap dengan sitasi `[1]`–`[6]` yang SAH (nomornya dari
`cite_add`), tetapi dokumen berakhir **tanpa Daftar Pustaka** — agen tidak punya
tool untuk menyusunnya, dan endpoint `regenerate-bibliography` hanya berjalan
bila pengguna menekannya sendiri di frontend.

Selain itu jurnal yang diunggah pengguna hidup di tabel ``journal_references``
sementara `read_document` hanya menjangkau tabel ``documents``, sehingga agen
tidak pernah bisa membaca isi bacaan yang justru ia sitasi.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.services.citation_tools import (
    _cari_referensi,
    _jenis_sumber,
    bibliografi_markdown,
    ref_read,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _RefPalsu:
    """Baris ``journal_references`` secukupnya untuk penomoran dan format IEEE."""

    def __init__(self, title, authors, year=2020, path="(tanpa berkas — sumber daring)",
                 abstract=None, journal="Jurnal Uji"):
        self.id = uuid.uuid4()
        self.title = title
        self.authors = authors
        self.year = year
        self.journal_name = journal
        self.volume = None
        self.issue = None
        self.pages = None
        self.doi = None
        self.publisher = None
        self.abstract = abstract
        self.file_path = path
        self.filename = path


_DAFTAR = [
    _RefPalsu("Intelligent Tutoring Systems: A Meta-Analysis", ["Wenting Ma"]),
    _RefPalsu("Attention Is All You Need", ["Ashish Vaswani"], 2017,
              path="https://arxiv.org/abs/1706.03762"),
    _RefPalsu("Knowledge Tracing: A Survey", ["Ghodai Abdelrahman"], 2023),
]


@pytest.fixture
def perpustakaan(monkeypatch):
    """Ganti akses DB dengan daftar tetap; penomoran murni soal urutan."""
    async def urut_palsu(_db, _user_id):
        return list(_DAFTAR)

    import app.services.citation_tools as ct

    monkeypatch.setattr(ct, "referensi_urut", urut_palsu)
    return _DAFTAR


@pytest.mark.anyio
async def test_daftar_pustaka_hanya_memuat_sitasi_yang_benar_benar_ditulis(perpustakaan):
    """Nomor yang tidak dipakai di naskah tidak boleh muncul di Daftar Pustaka."""
    naskah = "ITS efektif [1]. Transformer memakai attention [2]. Lagi-lagi [1]."

    md, dipakai, asing = await bibliografi_markdown(None, uuid.uuid4(), naskah)

    assert dipakai == [1, 2], md
    assert asing == []
    assert md.startswith("## DAFTAR PUSTAKA")
    assert "Attention Is All You Need" in md
    # [3] tidak dirujuk di naskah → tidak ikut.
    assert "Knowledge Tracing" not in md
    # Setiap entri diberi nomor yang sama dengan yang ada di teks.
    assert "[1] " in md and "[2] " in md and "[3] " not in md


@pytest.mark.anyio
async def test_nomor_di_luar_perpustakaan_dilaporkan_bukan_dikarang(perpustakaan):
    """Sitasi yang tak punya sumber harus terlihat, bukan diam-diam diformat."""
    naskah = "Klaim dengan nomor karangan [99] dan yang sah [1]."

    md, dipakai, asing = await bibliografi_markdown(None, uuid.uuid4(), naskah)

    assert dipakai == [1]
    assert asing == [99], "nomor asing wajib dilaporkan ke pemanggil"
    assert "99" not in md


@pytest.mark.anyio
async def test_naskah_tanpa_sitasi_tidak_menghasilkan_daftar_pustaka_kosong(perpustakaan):
    """Bab tanpa rujukan tidak boleh mendapat judul Daftar Pustaka hampa."""
    md, dipakai, asing = await bibliografi_markdown(None, uuid.uuid4(), "Paragraf tanpa rujukan.")

    assert md == ""
    assert dipakai == [] and asing == []


def test_acuan_sumber_boleh_nomor_kurung_judul_atau_id():
    """Agen merujuk sumber dengan cara paling alami baginya: nomor sitasinya."""
    assert _cari_referensi(_DAFTAR, "[2]")[1] == 2
    assert _cari_referensi(_DAFTAR, "2")[1] == 2
    assert _cari_referensi(_DAFTAR, "Attention Is All You Need")[1] == 2
    # Judul sebagian tetap ketemu.
    assert _cari_referensi(_DAFTAR, "knowledge tracing")[1] == 3
    assert _cari_referensi(_DAFTAR, str(_DAFTAR[0].id))[1] == 1
    # Di luar rentang / tak dikenal → None, bukan menebak.
    assert _cari_referensi(_DAFTAR, "[9]") is None
    assert _cari_referensi(_DAFTAR, "entah apa") is None


def test_jenis_sumber_membedakan_berkas_daring_dan_metadata(tmp_path):
    berkas = tmp_path / "jurnal.pdf"
    berkas.write_bytes(b"%PDF-1.4 dummy")
    assert _jenis_sumber(_RefPalsu("x", ["y"], path=str(berkas))) == "berkas"
    assert _jenis_sumber(_RefPalsu("x", ["y"], path="https://arxiv.org/abs/1")) == "daring"
    assert _jenis_sumber(_RefPalsu("x", ["y"])) == "metadata"
    # Path yang tercatat tapi berkasnya hilang bukan "berkas".
    assert _jenis_sumber(_RefPalsu("x", ["y"], path=str(tmp_path / "hilang.pdf"))) == "metadata"


@pytest.mark.anyio
async def test_ref_read_sumber_daring_mengarahkan_ke_fetch_webpage(perpustakaan):
    """Isi sumber daring tidak ada di server; agen harus diberi URL-nya."""
    hasil = json.loads(await ref_read(None, uuid.uuid4(), reference="[2]"))

    assert hasil["sitasi"] == "[2]"
    assert hasil["sumber"] == "daring"
    assert hasil["url"] == "https://arxiv.org/abs/1706.03762"
    assert "fetch_webpage" in hasil["pesan"]


@pytest.mark.anyio
async def test_ref_read_tanpa_isi_melarang_mengarang(perpustakaan):
    """Metadata saja bukan izin mengarang isi bacaan."""
    hasil = json.loads(await ref_read(None, uuid.uuid4(), reference="[1]"))

    assert hasil["sumber"] == "metadata saja"
    assert "mengarang" in hasil["pesan"]


@pytest.mark.anyio
async def test_ref_read_sumber_tak_dikenal_menyebut_yang_tersedia(perpustakaan):
    hasil = json.loads(await ref_read(None, uuid.uuid4(), reference="[42]"))

    assert "error" in hasil
    assert len(hasil["tersedia"]) == 3
    assert hasil["tersedia"][0]["sitasi"] == "[1]"
