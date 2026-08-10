"""Layanan Agentic Tool Dispatcher disalin dan disederhanakan dari Nalar AI agentic loop (nalar-ai/core/agentic/loop.py & nalar-ai/tools/write_note.py).

Modul ini memfasilitasi pembuatan catatan otomatis (Notebooks) dari percakapan AI serta penanganan multi-tool dispatch.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notebook import Notebook

logger = logging.getLogger(__name__)


async def write_learning_note(
    db: AsyncSession,
    user_id: uuid.UUID,
    title: str,
    content: str,
) -> Notebook:
    """Menyimpan ringkasan/catatan hasil belajar otomatis ke dalam tabel Notebooks pengguna (disalin dari Nalar AI write_note tool)."""
    note = Notebook(
        user_id=user_id,
        title=title,
        content=content,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    logger.info("Catatan otomatis berhasil dibuat untuk user %s: %s", user_id, title)
    return note


__all__ = ["write_learning_note"]
