"""Setelan capability per user (chat, solve, research, question, dll).

Kontrak DTO mengikuti ``app/(utility)/settings/capabilities/page.tsx``
(``CapabilitiesSettingsDTO``) yang menyalin bentuk
``nalar-ai/services/config/capabilities_settings.py``. Nilai bawaan di sini
adalah default yang masuk akal untuk aplikasi ini; baris tabel hanya menyimpan
penimpaan user. Blok yang diambil alur lain (mis. ``chat.temperature`` untuk
alur chat) dibaca lewat ``get_capability_block``.
"""

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_setting import CapabilitySetting

DEFAULT_CAPABILITY_SETTINGS: dict[str, dict[str, Any]] = {
    "chat": {
        "temperature": 0.7,
        "max_rounds": 3,
        "stage_budgets": {"exploring": 2, "responding": 1},
    },
    "solve": {
        "temperature": 0.2,
        "max_tokens": 4000,
        "max_rounds": 5,
        "max_replans": 2,
    },
    "research": {
        "temperature": 0.3,
        "max_tokens": 6000,
        "researching": {
            "note_agent_mode": "auto",
            "tool_timeout": 120,
            "tool_max_retries": 2,
            "paper_search_years_limit": 5,
        },
    },
    "question": {
        "temperature": 0.4,
        "max_tokens": 3000,
        "exploring": {
            "max_iterations": 3,
            "tool_summarizer": {"enabled": True, "max_tokens": 2000},
        },
    },
    "co_writer": {"temperature": 0.7, "max_tokens": 4000},
    "vision_solver": {"temperature": 0.2, "max_tokens": 1000},
    "math_animator": {"temperature": 0.2, "max_tokens": 4000},
}

CAPABILITY_NAMES = tuple(DEFAULT_CAPABILITY_SETTINGS.keys())


def _deep_merge(base: dict, override: dict) -> dict:
    """Gabung override ke base secara rekursif (nilai override menang)."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


async def get_or_create(db: AsyncSession, user_id: uuid.UUID) -> CapabilitySetting:
    row = await db.scalar(
        select(CapabilitySetting).where(CapabilitySetting.user_id == user_id)
    )
    if row is None:
        row = CapabilitySetting(user_id=user_id)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def get_capability_settings(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """Setelan lengkap: bawaan + penimpaan user (hanya kunci yang dikenal)."""
    row = await get_or_create(db, user_id)
    stored = row.load()
    merged = dict(DEFAULT_CAPABILITY_SETTINGS)
    for name, block in stored.items():
        if name in merged and isinstance(block, dict):
            merged[name] = _deep_merge(merged[name], block)
    return merged


async def save_capability_settings(
    db: AsyncSession, user_id: uuid.UUID, payload: dict
) -> dict:
    """Simpan penimpaan; kunci tak dikenal diabaikan, lalu kembalikan setelan lengkap."""
    row = await get_or_create(db, user_id)
    cleaned = {}
    for name, block in payload.items():
        if name in DEFAULT_CAPABILITY_SETTINGS and isinstance(block, dict):
            cleaned[name] = block
    row.save(cleaned)
    await db.commit()
    await db.refresh(row)
    return await get_capability_settings(db, user_id)


async def get_capability_block(
    db: AsyncSession, user_id: uuid.UUID, name: str
) -> dict:
    """Blok setelan satu capability (bawaan + penimpaan), untuk alur lain."""
    settings = await get_capability_settings(db, user_id)
    block = settings.get(name)
    return block if isinstance(block, dict) else {}
