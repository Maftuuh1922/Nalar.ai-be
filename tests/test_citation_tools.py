"""Penomoran sitasi `[n]` untuk agen Co-Writer.

Cacat yang dikunci di sini terukur pada basis data proyek ini: kolom
``journal_references.created_at`` memakai ``server_default=func.now()`` yang di
SQLite hanya berpresisi **detik**, sehingga sumber yang disimpan agen dalam satu
detik punya ``created_at`` identik (terukur 2 dari 2 baris). Pengurutan yang
bergantung pada ``created_at`` saja karena itu tidak stabil — dua sumber berbeda
sempat sama-sama mendapat ``[1]``, dan nomor bisa berpindah entri antar-permintaan
sehingga setiap ``[n]`` yang sudah tertulis di naskah jadi salah rujuk.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import delete, select

from app.db.session import AsyncSessionLocal
from app.models.journal import JournalGroup, JournalReference
from app.models.user import User
from app.services.citation_formatter import (
    citation_meta_from_reference,
    generate_citation,
)
from app.services.citation_tools import (
    NAMA_GRUP_AGEN,
    SitasiError,
    cite_add,
    cite_list,
    referensi_urut,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _user_uji(db) -> User:
    """User khusus tes; datanya dibersihkan sebelum dipakai."""
    email = "uji-sitasi@contoh.test"
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            username="uji-sitasi",
            email=email,
            hashed_password="x",
            full_name="Uji Sitasi",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    grup = list(await db.scalars(select(JournalGroup).where(JournalGroup.user_id == user.id)))
    for g in grup:
        await db.execute(delete(JournalReference).where(JournalReference.group_id == g.id))
        await db.execute(delete(JournalGroup).where(JournalGroup.id == g.id))
    await db.execute(delete(JournalReference).where(JournalReference.user_id == user.id))
    await db.commit()
    return user


@pytest.mark.anyio
async def test_sumber_berbeda_dapat_nomor_berbeda_walau_created_at_identik():
    """Tiga sumber yang disimpan berturut-turut wajib bernomor 1, 2, 3.

    Ini regresi langsung: dengan pengurutan `created_at` saja, dua panggilan
    `cite_add` dalam satu detik sama-sama mengembalikan `[1]`.
    """
    async with AsyncSessionLocal() as db:
        user = await _user_uji(db)
        a = json.loads(await cite_add(db, user.id, title="Attention Is All You Need",
                                      authors=["Ashish Vaswani"], year=2017))
        b = json.loads(await cite_add(db, user.id, title="Retrieval-Augmented Generation",
                                      authors=["Patrick Lewis"], year=2020))
        c = json.loads(await cite_add(db, user.id, title="BERT Pre-training",
                                      authors=["Jacob Devlin"], year=2019))

        assert [a["nomor"], b["nomor"], c["nomor"]] == [1, 2, 3], (a, b, c)
        assert a["sitasi"] == "[1]" and c["sitasi"] == "[3]"

        # created_at memang identik — itulah kondisi yang dulu merusak penomoran.
        daftar = await referensi_urut(db, user.id)
        assert len({r.created_at for r in daftar}) == 1, "prasyarat tes tidak terpenuhi"


@pytest.mark.anyio
async def test_nomor_lama_tidak_bergeser_saat_sumber_baru_ditambah():
    """Penomoran wajib append-only: `[n]` yang sudah tertulis tetap sah.

    Kalau nomor bergeser, setiap sitasi yang sudah diketik ke naskah berubah
    maknanya — untuk laporan akademik itu kerusakan senyap yang parah.
    """
    async with AsyncSessionLocal() as db:
        user = await _user_uji(db)
        a = json.loads(await cite_add(db, user.id, title="Sumber A", authors=["A A"]))
        b = json.loads(await cite_add(db, user.id, title="Sumber B", authors=["B B"]))
        json.loads(await cite_add(db, user.id, title="Sumber C", authors=["C C"]))
        baru = json.loads(await cite_add(db, user.id, title="Sumber D", authors=["D D"]))

        daftar = await referensi_urut(db, user.id)
        assert daftar[a["nomor"] - 1].title == "Sumber A"
        assert daftar[b["nomor"] - 1].title == "Sumber B"
        assert baru["nomor"] == len(daftar), "sumber baru harus dapat nomor terakhir"


@pytest.mark.anyio
async def test_sumber_sama_tidak_dapat_nomor_kedua():
    """DOI/judul sama mengembalikan nomor yang sudah ada, bukan entri baru."""
    async with AsyncSessionLocal() as db:
        user = await _user_uji(db)
        asli = json.loads(await cite_add(
            db, user.id, title="Attention Is All You Need",
            authors=["Ashish Vaswani"], doi="10.48550/arXiv.1706.03762"))

        # DOI sama tapi ditulis sebagai URL, judul beda kapitalisasi.
        sama_doi = json.loads(await cite_add(
            db, user.id, title="ATTENTION IS ALL YOU NEED",
            authors=["A. Vaswani"], doi="https://doi.org/10.48550/arXiv.1706.03762"))
        assert sama_doi["status"] == "sudah_ada"
        assert sama_doi["nomor"] == asli["nomor"]

        # Judul sama tanpa DOI juga terdeteksi.
        sama_judul = json.loads(await cite_add(
            db, user.id, title="Attention is all you need!", authors=["Lain Orang"]))
        assert sama_judul["nomor"] == asli["nomor"]

        assert len(await referensi_urut(db, user.id)) == 1


@pytest.mark.anyio
async def test_sumber_tanpa_judul_atau_penulis_ditolak():
    """Metadata tak lengkap ditolak supaya Daftar Pustaka tidak berisi entri kosong."""
    async with AsyncSessionLocal() as db:
        user = await _user_uji(db)
        with pytest.raises(SitasiError):
            await cite_add(db, user.id, title="", authors=["A A"])
        with pytest.raises(SitasiError):
            await cite_add(db, user.id, title="Ada Judul", authors=[])
        assert await referensi_urut(db, user.id) == []


@pytest.mark.anyio
async def test_nomor_cocok_dengan_entri_daftar_pustaka_yang_dicetak():
    """`[n]` dari cite_add harus menunjuk entri IEEE ke-n — ujung ke ujung."""
    async with AsyncSessionLocal() as db:
        user = await _user_uji(db)
        json.loads(await cite_add(db, user.id, title="Sumber Pertama",
                                  authors=["Satu Orang"], year=2020, journal="Jurnal A"))
        kedua = json.loads(await cite_add(db, user.id, title="Sumber Kedua",
                                          authors=["Dua Orang"], year=2021, journal="Jurnal B"))

        daftar = await referensi_urut(db, user.id)
        entri = generate_citation(
            citation_meta_from_reference(daftar[kedua["nomor"] - 1]), "ieee"
        )
        assert "Sumber Kedua" in entri, entri

        daftar_json = json.loads(await cite_list(db, user.id))
        assert daftar_json["jumlah"] == 2
        assert daftar_json["referensi"][kedua["nomor"] - 1]["sitasi"] == kedua["sitasi"]
