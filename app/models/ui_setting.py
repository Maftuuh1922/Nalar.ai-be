"""Model tabel ui_settings — katalog layanan AI & preferensi tampilan per user.

Satu baris per user. Katalog (profil + daftar model untuk llm, embedding,
search, tts, stt, imagegen, videogen) disimpan sebagai JSON string di kolom
``catalog_json``, dan preferensi tampilan (tema, bahasa, gaya code block) di
``ui_json``.

Sebelumnya katalog hanya hidup di variabel global proses dan file
``catalog.json`` relatif terhadap CWD, sehingga hilang setiap backend
di-restart dan bocor antar user. Menyimpannya di ``nalar_ai.db`` membuat model
yang disimpan lewat halaman Pengaturan tetap ada dan terbaca kembali.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UiSetting(Base):
    __tablename__ = "ui_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    # Katalog layanan AI sebagai JSON string.
    catalog_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    # Preferensi tampilan sebagai JSON string.
    ui_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
