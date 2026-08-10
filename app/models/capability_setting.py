"""Model tabel capability_settings.

Satu baris per user berisi JSON setelan tiap capability (chat, solve,
research, question, co_writer, vision_solver, math_animator). Nilai bawaan
didefinisikan di ``app/services/capability_settings.py``; baris ini hanya
menyimpan penimpaan (override) yang dikirim halaman Pengaturan > Capabilities.
"""

import json
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CapabilitySetting(Base):
    __tablename__ = "capability_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, primary_key=True,
    )
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def load(self) -> dict:
        try:
            return json.loads(self.settings_json or "{}")
        except (ValueError, TypeError):
            return {}

    def save(self, payload: dict) -> None:
        self.settings_json = json.dumps(payload)
