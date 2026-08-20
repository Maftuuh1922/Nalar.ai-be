"""Tool sitasi untuk agen Co-Writer: simpan sumber → dapat nomor `[n]` yang sah.

Latar masalahnya: laporan akademik di proyek ini memakai sitasi bergaya IEEE
(`[1]`, `[2]`, …) dan endpoint `regenerate-bibliography` membangun Daftar Pustaka
dengan memindai pola `\\[(\\d+)\\]` di naskah, lalu memetakan nomor itu ke tabel
``journal_references`` **urut `created_at`**. Sementara itu prompt agen (dengan
benar) melarang mengarang nomor sitasi, sehingga agen tidak pernah menulis `[n]`
sama sekali — pemindaian selalu menemukan nol dan Daftar Pustaka tetap kosong.
Terukur: perpustakaan referensi berisi 0 baris meski dokumen sudah lengkap.

Modul ini menutup celah itu tanpa membuka pintu halusinasi: nomor `[n]` **tidak
pernah** berasal dari model. Agen mengirim metadata sumber yang sudah
diverifikasi (hasil `search_web`/`arxiv_search`/`fetch_webpage`), server
menyimpannya, lalu server-lah yang mengembalikan nomor urutnya. Model hanya
menyalin nomor yang diberikan server.

Lihat [[nalar-agentic-nulis-sebelum-rencana]] untuk pola penegakan tool serupa.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.journal import JournalGroup, JournalReference

logger = logging.getLogger(__name__)

# Kolom pemecah seri untuk penomoran sitasi. SQLite punya `rowid` implisit yang
# monotonik naik sesuai urutan penyisipan — tepat untuk penomoran append-only.
# PostgreSQL tidak punya padanannya, jadi di sana dipakai `created_at` + `id`;
# `created_at` PostgreSQL berpresisi mikrodetik sehingga tabrakan praktis tak
# terjadi (masalah presisi detik itu khas SQLite).
_MEMAKAI_SQLITE = settings.DATABASE_URL.startswith("sqlite")
_KOLOM_URUT_SISIP = column("rowid") if _MEMAKAI_SQLITE else JournalReference.id

# Grup tempat sumber temuan agen disimpan. Dibuat sekali per user bila belum ada;
# referensi wajib punya group_id (NOT NULL) sedangkan sumber hasil riset web tidak
# berasal dari unggahan berkas mana pun.
NAMA_GRUP_AGEN = "Sumber Riset Agentic"

# Sumber tanpa berkas tetap harus mengisi filename/file_path (NOT NULL di model).
# Penanda ini membuatnya jelas bukan berkas nyata, sehingga jalur yang membaca
# file_path tidak mengira ada PDF yang bisa dibuka.
_TANPA_BERKAS = "(tanpa berkas — sumber daring)"


class SitasiError(ValueError):
    """Metadata sumber tidak cukup untuk dijadikan referensi yang bisa dirujuk."""


def _bersih(nilai: object) -> str:
    return " ".join(str(nilai or "").split())


def _normal_doi(doi: object) -> str:
    """DOI dinormalkan agar pembandingan duplikat tidak tertipu awalan URL."""
    teks = _bersih(doi).lower()
    teks = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", teks)
    return teks


def _normal_judul(judul: object) -> str:
    """Judul dinormalkan (huruf kecil, tanpa tanda baca) untuk deteksi duplikat."""
    return re.sub(r"[^a-z0-9]+", " ", _bersih(judul).lower()).strip()


def _daftar_penulis(nilai: object) -> list[str]:
    if isinstance(nilai, str):
        pecah = [p.strip() for p in re.split(r";|\band\b|,(?=\s*[A-Z])", nilai)]
        return [p for p in pecah if p]
    if isinstance(nilai, (list, tuple)):
        return [_bersih(p) for p in nilai if _bersih(p)]
    return []


async def _grup_agen(db: AsyncSession, user_id: uuid.UUID) -> JournalGroup:
    """Ambil (atau buat) grup penampung sumber temuan agen."""
    grup = await db.scalar(
        select(JournalGroup).where(
            JournalGroup.user_id == user_id,
            JournalGroup.name == NAMA_GRUP_AGEN,
        )
    )
    if grup is not None:
        return grup
    grup = JournalGroup(
        user_id=user_id,
        name=NAMA_GRUP_AGEN,
        description=(
            "Sumber yang ditemukan dan diverifikasi oleh Asisten Agentic saat "
            "menulis draf. Nomor sitasi [n] mengikuti urutan penambahan."
        ),
    )
    db.add(grup)
    await db.flush()
    return grup


async def referensi_urut(db: AsyncSession, user_id: uuid.UUID) -> list[JournalReference]:
    """Referensi user dalam urutan yang menentukan nomor sitasi `[n]`.

    SATU sumber kebenaran untuk penomoran: dipakai `cite_add`/`cite_list` maupun
    `regenerate-bibliography`, ekspor DOCX, dan ekspor dokumen. Kalau ada jalur
    yang mengurut sendiri, nomor yang dijanjikan ke agen bisa merujuk entri
    Daftar Pustaka yang lain.

    Diurutkan `(created_at, rowid)`. `created_at` saja TIDAK cukup: kolom itu
    memakai `server_default=func.now()` yang di SQLite hanya berpresisi detik,
    jadi sumber yang ditambahkan dalam satu detik punya `created_at` identik —
    terukur 2 dari 2 baris identik saat agen menyimpan beberapa sumber berturut.
    Tanpa pemecah seri yang stabil, urutannya mengikuti apa pun yang dikembalikan
    DB, sehingga `[1]` bisa berpindah entri antar-permintaan dan setiap nomor yang
    sudah tertulis di naskah jadi salah rujuk.

    `rowid` dipakai (bukan `str(id)`) karena ia monotonik naik sesuai urutan
    penyisipan, sehingga penomoran bersifat **append-only**: sumber baru selalu
    mendapat nomor terakhir dan nomor lama tidak pernah bergeser.
    """
    rows = list(
        await db.scalars(
            select(JournalReference)
            .where(JournalReference.user_id == user_id)
            .order_by(_KOLOM_URUT_SISIP, JournalReference.created_at)
        )
    )
    return rows


def _cari_duplikat(
    daftar: list[JournalReference], doi: str, judul: str
) -> JournalReference | None:
    """Sumber yang sama tidak boleh dapat dua nomor berbeda."""
    if doi:
        for r in daftar:
            if _normal_doi(r.doi) == doi:
                return r
    if judul:
        for r in daftar:
            if _normal_judul(r.title) == judul:
                return r
    return None


async def cite_add(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    title: str = "",
    authors: Any = None,
    year: Any = None,
    journal: str = "",
    doi: str = "",
    url: str = "",
    **kwargs,
) -> str:
    """Simpan satu sumber ke perpustakaan referensi, kembalikan nomor `[n]`-nya.

    Nomor dihitung server dari urutan `created_at`, sama seperti yang dipakai
    `regenerate-bibliography`, sehingga `[n]` yang ditulis agen dijamin merujuk
    entri Daftar Pustaka yang benar. Sumber duplikat (DOI atau judul sama)
    mengembalikan nomor yang sudah ada, bukan membuat entri baru.
    """
    judul = _bersih(title)
    penulis = _daftar_penulis(authors)
    if not judul:
        raise SitasiError("Judul sumber wajib ada — jangan menyimpan sumber tanpa judul.")
    if not penulis:
        raise SitasiError(
            "Daftar penulis wajib ada. Ambil dari hasil pencarian; jangan mengarang."
        )

    doi_bersih = _normal_doi(doi)
    judul_norm = _normal_judul(judul)

    daftar = await referensi_urut(db, user_id)
    lama = _cari_duplikat(daftar, doi_bersih, judul_norm)
    if lama is not None:
        nomor = daftar.index(lama) + 1
        return json.dumps({
            "status": "sudah_ada",
            "nomor": nomor,
            "sitasi": f"[{nomor}]",
            "title": lama.title,
            "pesan": "Sumber ini sudah ada di perpustakaan; pakai nomor tersebut.",
        }, ensure_ascii=False)

    tahun: int | None = None
    try:
        if year is not None and _bersih(year):
            tahun = int(re.sub(r"\D", "", _bersih(year))[:4] or 0) or None
    except (TypeError, ValueError):
        tahun = None

    grup = await _grup_agen(db, user_id)
    ref = JournalReference(
        user_id=user_id,
        group_id=grup.id,
        filename=_bersih(url) or _TANPA_BERKAS,
        file_path=_bersih(url) or _TANPA_BERKAS,
        title=judul,
        authors=penulis,
        year=tahun,
        journal_name=_bersih(journal),
        doi=_bersih(doi) or None,
        status="extracted",
    )
    db.add(ref)
    await db.commit()
    await db.refresh(ref)

    daftar = await referensi_urut(db, user_id)
    nomor = next((i + 1 for i, r in enumerate(daftar) if r.id == ref.id), len(daftar))
    logger.info("cite_add: '%s' disimpan sebagai [%s] untuk user %s", judul[:60], nomor, user_id)
    return json.dumps({
        "status": "ditambahkan",
        "nomor": nomor,
        "sitasi": f"[{nomor}]",
        "title": judul,
        "pesan": (
            f"Tulis tepat \"[{nomor}]\" di teks pada tempat klaim ini dirujuk. "
            "Jangan menulis nomor lain."
        ),
    }, ensure_ascii=False)


async def cite_list(db: AsyncSession, user_id: uuid.UUID, **kwargs) -> str:
    """Daftar sumber yang sudah tersimpan beserta nomor `[n]`-nya.

    Dipakai agen sebelum menambah sumber baru, supaya klaim yang merujuk sumber
    yang sama memakai nomor yang sama alih-alih menggandakan entri. Juga jalan
    masuk ke perpustakaan jurnal yang diunggah pengguna sendiri: entri yang
    punya berkas nyata bisa dibaca isinya lewat `ref_read`.
    """
    daftar = await referensi_urut(db, user_id)
    if not daftar:
        return json.dumps({
            "jumlah": 0,
            "referensi": [],
            "pesan": "Perpustakaan masih kosong. Pakai cite_add setelah memverifikasi sumber.",
        }, ensure_ascii=False)
    return json.dumps({
        "jumlah": len(daftar),
        "referensi": [
            {
                "nomor": i + 1,
                "sitasi": f"[{i + 1}]",
                "title": r.title[:160],
                "year": r.year,
                "authors": (r.authors or [])[:3],
                # Penanda apakah isi sumber bisa dibaca: berkas unggahan bisa
                # diekstrak, sumber daring perlu fetch_webpage.
                "bisa_dibaca": _jenis_sumber(r),
            }
            for i, r in enumerate(daftar)
        ],
    }, ensure_ascii=False)


def _jenis_sumber(ref: JournalReference) -> str:
    """Bagaimana isi sumber ini bisa diakses: berkas, daring, atau metadata saja."""
    jalur = str(ref.file_path or "")
    if jalur.startswith(("http://", "https://")):
        return "daring"
    if jalur and jalur != _TANPA_BERKAS and Path(jalur).exists():
        return "berkas"
    return "metadata"


def _cari_referensi(
    daftar: list[JournalReference], acuan: str
) -> tuple[JournalReference, int] | None:
    """Resolusi acuan agen (nomor `[3]`/`3`, judul, atau id) ke satu referensi."""
    acuan = _bersih(acuan)
    if not acuan:
        return None

    angka = re.fullmatch(r"\[?(\d+)\]?", acuan)
    if angka:
        idx = int(angka.group(1)) - 1
        if 0 <= idx < len(daftar):
            return daftar[idx], idx + 1
        return None

    for i, r in enumerate(daftar):
        if str(r.id) == acuan:
            return r, i + 1

    ternorm = _normal_judul(acuan)
    for i, r in enumerate(daftar):
        if _normal_judul(r.title) == ternorm:
            return r, i + 1
    for i, r in enumerate(daftar):
        if ternorm and ternorm in _normal_judul(r.title):
            return r, i + 1
    return None


async def ref_read(
    db: AsyncSession,
    user_id: uuid.UUID,
    reference: str = "",
    max_chars: int = 6000,
    **kwargs,
) -> str:
    """Baca isi satu sumber di perpustakaan referensi pengguna.

    Melengkapi `read_document` yang hanya menjangkau tabel ``documents``: jurnal
    yang diunggah pengguna hidup di ``journal_references`` dan sebelumnya sama
    sekali tidak bisa dibaca agen — agen hanya melihat metadatanya lewat
    `cite_list`, sehingga tidak bisa menulis tinjauan pustaka yang benar-benar
    bersumber dari bacaan itu.

    ``reference`` menerima nomor sitasi (`3` atau `[3]`), judul, atau id.
    """
    daftar = await referensi_urut(db, user_id)
    if not daftar:
        return json.dumps({
            "error": "Perpustakaan referensi masih kosong.",
        }, ensure_ascii=False)

    ketemu = _cari_referensi(daftar, reference)
    if ketemu is None:
        return json.dumps({
            "error": f"Sumber '{reference}' tidak ada di perpustakaan.",
            "tersedia": [
                {"sitasi": f"[{i + 1}]", "title": r.title[:100]}
                for i, r in enumerate(daftar)
            ],
        }, ensure_ascii=False)

    ref, nomor = ketemu
    dasar: dict[str, Any] = {
        "sitasi": f"[{nomor}]",
        "title": ref.title,
        "authors": ref.authors or [],
        "year": ref.year,
        "journal": ref.journal_name or "",
        "doi": ref.doi or "",
    }

    jenis = _jenis_sumber(ref)
    if jenis == "berkas":
        try:
            teks = await asyncio.to_thread(_ekstrak_teks_berkas, str(ref.file_path))
        except Exception as e:  # noqa: BLE001
            logger.warning("ref_read gagal mengekstrak %s: %s", ref.file_path, e)
            teks = ""
        if teks.strip():
            penuh = len(teks)
            return json.dumps({
                **dasar,
                "sumber": "berkas unggahan",
                "total_karakter": penuh,
                "content": teks[:max_chars],
                "catatan": (
                    "Dipotong; panggil lagi dengan max_chars lebih besar bila perlu."
                    if penuh > max_chars else ""
                ),
            }, ensure_ascii=False)

    if ref.abstract and ref.abstract.strip():
        return json.dumps({
            **dasar,
            "sumber": "abstrak tersimpan",
            "content": ref.abstract.strip()[:max_chars],
        }, ensure_ascii=False)

    if jenis == "daring":
        return json.dumps({
            **dasar,
            "sumber": "daring",
            "url": str(ref.file_path),
            "pesan": (
                "Isi tidak tersimpan di server. Panggil fetch_webpage dengan url di "
                "atas untuk membaca isinya."
            ),
        }, ensure_ascii=False)

    return json.dumps({
        **dasar,
        "sumber": "metadata saja",
        "pesan": (
            "Hanya metadata yang tersedia. Jangan mengarang isi sumber ini; sitasi "
            "tetap boleh dipakai, tetapi klaimnya harus berasal dari sumber lain "
            "yang benar-benar kamu baca."
        ),
    }, ensure_ascii=False)


def _ekstrak_teks_berkas(jalur: str) -> str:
    """Ekstrak teks berkas jurnal. Diimpor malas: LlamaIndex berat dimuat."""
    from llama_index.core import SimpleDirectoryReader

    bagian: list[str] = []
    for doc in SimpleDirectoryReader(input_files=[jalur]).load_data():
        if not doc.text:
            continue
        label = (doc.metadata or {}).get("page_label")
        bagian.append(f"[Halaman {label}]\n{doc.text}" if label else doc.text)
    return "\n\n".join(bagian)


async def bibliografi_markdown(
    db: AsyncSession,
    user_id: uuid.UUID,
    naskah: str,
    *,
    format_name: str = "ieee",
) -> tuple[str, list[int], list[int]]:
    """Bangun blok Daftar Pustaka dari sitasi `[n]` yang BENAR-BENAR dipakai.

    Mengembalikan ``(markdown, nomor_dipakai, nomor_tak_dikenal)``. Hanya nomor
    yang muncul di naskah yang masuk, dan penomorannya memakai `referensi_urut`
    sehingga cocok dengan yang dijanjikan `cite_add` maupun yang dihasilkan
    endpoint `regenerate-bibliography`.
    """
    from app.services.citation_formatter import (
        citation_meta_from_reference,
        generate_citation,
    )

    daftar = await referensi_urut(db, user_id)
    dipakai = sorted({int(m) for m in re.findall(r"\[(\d+)\]", naskah or "")})
    sah = [n for n in dipakai if 1 <= n <= len(daftar)]
    asing = [n for n in dipakai if n not in sah]

    if not sah:
        return "", [], asing

    baris = ["## DAFTAR PUSTAKA", ""]
    for n in sah:
        ref = daftar[n - 1]
        try:
            teks = generate_citation(citation_meta_from_reference(ref), format_name)
        except Exception as e:  # noqa: BLE001
            logger.warning("format sitasi [%s] gagal: %s", n, e)
            teks = _bersih(ref.title)
        baris.append(f"[{n}] {teks}")
        baris.append("")
    return "\n".join(baris).rstrip() + "\n", sah, asing


__all__ = [
    "NAMA_GRUP_AGEN",
    "SitasiError",
    "bibliografi_markdown",
    "cite_add",
    "cite_list",
    "ref_read",
    "referensi_urut",
]
