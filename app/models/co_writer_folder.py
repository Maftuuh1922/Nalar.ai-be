"""Model tabel co_writer_folders (folder bertingkat untuk draf Co-Writer).

Mengikuti pola `JournalGroup`: UUID pk, `user_id` FK CASCADE, `created_at`
server default. Bedanya folder di sini boleh bersarang, lewat `parent_id` yang
menunjuk ke tabelnya sendiri.

Catatan penting soal integritas: basis data proyek ini (SQLite) berjalan tanpa
`PRAGMA foreign_keys=ON` — lihat `app/db/session.py`. Artinya klausa
`ondelete` di bawah TIDAK ditegakkan oleh mesin basis data. Konsekuensinya:
- menghapus folder harus memindahkan subfolder dan dokumen anaknya secara
  eksplisit di kode route, kalau tidak keduanya jadi yatim dan lenyap dari UI;
- kedalaman dan siklus (folder dipindah ke dalam keturunannya sendiri) juga
  dijaga di route, bukan di skema.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CoWriterFolder(Base):
    __tablename__ = "co_writer_folders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # NULL = folder akar. Kedalaman dibatasi di route (MAX_DEPTH), bukan di skema.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("co_writer_folders.id"),
        nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Warna aksen opsional, format "#rrggbb".
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
