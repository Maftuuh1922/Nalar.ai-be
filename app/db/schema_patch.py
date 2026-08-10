"""Penambahan kolom untuk tabel yang sudah ada di basis data.

Skema proyek ini dibangun oleh `Base.metadata.create_all` di `app.main.lifespan`,
bukan oleh Alembic: basis data `nalar_ai.db` bahkan tidak punya tabel
`alembic_version`, dan tak satu pun berkas di `alembic/versions/` yang membuat
tabel Co-Writer. Konsekuensinya, `create_all` MEMBUAT tabel yang belum ada tapi
tidak pernah MENGUBAH tabel yang sudah ada — kolom baru pada model yang tabelnya
sudah terpasang tidak akan muncul, dan kueri gagal dengan "no such column".

Modul ini menutup celah itu: daftar kolom tambahan dijalankan sebagai
`ALTER TABLE ... ADD COLUMN` yang idempoten (dilewati bila kolomnya sudah ada),
tepat setelah `create_all`.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

# (tabel, kolom, definisi DDL) — definisi harus punya DEFAULT bila NOT NULL,
# karena SQLite menolak menambah kolom NOT NULL tanpa nilai bawaan ke tabel
# yang sudah berisi baris.
_KOLOM_TAMBAHAN: list[tuple[str, str, str]] = [
    # Penanda apakah isi draf sudah dikonversi ke LaTeX. Draf yang sudah ada
    # sebelum perubahan ini berisi Markdown, jadi bawaannya "markdown" dan
    # get_document yang mengkonversinya sekali.
    ("co_writer_documents", "content_format", "VARCHAR(16) NOT NULL DEFAULT 'markdown'"),
    # Lapis 1 (PRD v2.8 §2): AST dokumen (JSON) — preview dirender dari sini.
    ("co_writer_documents", "structured_content", "TEXT"),
]


def _terapkan(conn) -> None:
    """Jalankan ADD COLUMN yang belum ada (dipanggil lewat run_sync)."""
    inspector = inspect(conn)
    tabel_ada = set(inspector.get_table_names())
    for tabel, kolom, definisi in _KOLOM_TAMBAHAN:
        if tabel not in tabel_ada:
            continue  # create_all baru saja membuatnya lengkap dengan kolomnya.
        if kolom in {c["name"] for c in inspector.get_columns(tabel)}:
            continue
        conn.execute(text(f"ALTER TABLE {tabel} ADD COLUMN {kolom} {definisi}"))
        logger.info("Kolom %s.%s ditambahkan.", tabel, kolom)


async def terapkan_kolom_tambahan(conn: AsyncConnection) -> None:
    """Selaraskan tabel lama dengan kolom baru pada model."""
    await conn.run_sync(_terapkan)
