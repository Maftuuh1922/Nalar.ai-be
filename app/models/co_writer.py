"""Model tabel co_writer_documents (draf Co-Writer)."""

import uuid
from datetime import datetime

from sqlalchemy import Uuid, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CoWriterDocument(Base):
    __tablename__ = "co_writer_documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Lapis 1 (PRD v2.8 §2): AST dokumen (JSON). Sumber kebenaran struktur —
    # preview dirender dari sini; `content` tetap dipakai editor dan ekspor.
    structured_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Penanda "sudah dikonversi ke LaTeX atau belum", bukan percabangan format.
    # Draf lama lahir sebagai "markdown"; get_document mengkonversinya sekali
    # lalu menyetel "latex". Dokumen baru langsung "latex".
    content_format: Mapped[str] = mapped_column(String(16), nullable=False, default="latex", server_default="markdown")
    # SFDT (Syncfusion Document Editor, JSON) — representasi kerja editor ala
    # Word. Kosong bila dokumen belum pernah dibuka/dikonversi di editor baru.
    sfdt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
