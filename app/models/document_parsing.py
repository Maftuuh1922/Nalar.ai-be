"""Model tabel document_parsing_settings.

Satu baris per user: engine aktif, opsi per engine (JSON), dan setelan MinerU
termasuk token API (terenkripsi). Opsi disimpan sebagai JSON karena bentuknya
berbeda-beda antar engine (docling, markitdown, pymupdf4llm, mineru).
"""

import json
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentParsingSetting(Base):
    __tablename__ = "document_parsing_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, primary_key=True,
    )
    # Engine aktif. Disinkronkan juga ke user_preferences.document_parsing_engine
    # supaya kolom lama itu tetap bermakna bagi alur parsing lain.
    engine: Mapped[str] = mapped_column(String(30), nullable=False, default="text_only")
    # JSON: {"docling": {...}, "markitdown": {...}, "pymupdf4llm": {...}}
    options_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # JSON: {"settings": {...}, "api_token_encrypted": "..."}
    mineru_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def load_options(self) -> dict:
        try:
            return json.loads(self.options_json or "{}")
        except (ValueError, TypeError):
            return {}

    def save_options(self, options: dict) -> None:
        self.options_json = json.dumps(options)

    def load_mineru(self) -> dict:
        try:
            return json.loads(self.mineru_json or "{}")
        except (ValueError, TypeError):
            return {}

    def save_mineru(self, payload: dict) -> None:
        self.mineru_json = json.dumps(payload)
