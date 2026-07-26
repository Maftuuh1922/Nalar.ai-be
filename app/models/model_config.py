"""Model tabel model_configs — konfigurasi LLM per user."""

import uuid
from datetime import datetime

from sqlalchemy import Uuid, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="Konfigurasi AI")
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Kemampuan model, disimpan sebagai JSON array string: ["text", "vision", ...].
    # Dipakai untuk memilih model yang tepat, mis. hanya model "vision" yang
    # boleh menerima lampiran gambar.
    capabilities: Mapped[str] = mapped_column(Text, nullable=False, default='["text"]', server_default='["text"]')
    # Jenis penyedia: openai-compatible / google / anthropic / ollama.
    provider_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="openai-compatible", server_default="openai-compatible"
    )
    # Perkiraan jendela konteks (token) untuk memangkas riwayat percakapan.
    context_window: Mapped[int] = mapped_column(Integer, nullable=False, default=65536, server_default="65536")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
