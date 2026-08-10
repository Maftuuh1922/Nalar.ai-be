"""Model tabel user_preferences.

Satu baris per user, berisi setelan yang sebelumnya hanya tampil sebagai
kontrol kosong di modal Pengaturan. Semua kolom di sini benar-benar dibaca
oleh alur chat, indexing dokumen, dan Riset Mendalam.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserPreference(Base):
    __tablename__ = "user_preferences"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    # --- Percakapan ---
    chat_temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    chat_max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=8000)
    history_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    enable_web_tools: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enable_document_tools: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enable_suggestions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- Pusat Pengetahuan (indexing & pencarian dokumen) ---
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, default=512)
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False, default=64)
    retrieval_top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    document_parsing_engine: Mapped[str] = mapped_column(String(30), nullable=False, default="text_only")
    rag_engine: Mapped[str] = mapped_column(String(30), nullable=False, default="llamaindex")

    # --- Jaringan ---
    request_timeout: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    proxy_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    bypass_proxy_local: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- Riset Mendalam & Latihan Soal ---
    research_default_depth: Mapped[str] = mapped_column(String(20), nullable=False, default="standar")
    default_quiz_questions: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    # Catatan bebas yang selalu ditempelkan ke system prompt chat.
    custom_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
