"""Model tabel research_reports (Riset Mendalam).

Satu baris mewakili satu permintaan riset: dari topik yang diketik user sampai
laporan panjang yang selesai ditulis. Proses berjalan di latar belakang, jadi
kolom progres ikut disimpan agar frontend bisa menampilkan tahapannya.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ResearchReport(Base):
    __tablename__ = "research_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    topic: Mapped[str] = mapped_column(String(500), nullable=False)
    # Petunjuk tambahan dari user, misal "fokus ke penerapan di Indonesia".
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    depth: Mapped[str] = mapped_column(String(20), nullable=False, default="standar")

    # pending | running | completed | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    progress_step: Mapped[str] = mapped_column(String(255), nullable=False, default="Menunggu antrean")
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    outline: Mapped[list | None] = mapped_column(JSON, nullable=True)
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
