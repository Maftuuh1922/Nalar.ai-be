"""Logika halaman Document Parsing: meta engine, deteksi instalasi, readiness.

Kontrak payload mengikuti ``app/(utility)/settings/document-parsing/page.tsx``:

    {
      "engine": str,
      "engines": {engine_id: {opsi...}},
      "available_engines": [{id, name, description, needs_local_models, available}],
      "readiness": {engine_id: {ready, reason, message}},
      "installable": [engine_id...],
      "mineru": {"api_token_set": bool, "local_cli": {...}},
    }
"""

import importlib.util
import json
import os
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_api_key, encrypt_api_key
from app.models.document_parsing import DocumentParsingSetting
from app.models.user import User
from app.models.user_preference import UserPreference

ENGINE_IDS = ("text_only", "pymupdf4llm", "markitdown", "docling", "mineru")

ENGINE_META: dict[str, dict] = {
    "text_only": {
        "name": "Text-only",
        "description": (
            "Built-in plain text extraction for PDF, Office, and text files. "
            "No optional parser package, model download, OCR, or layout reconstruction."
        ),
        "needs_local_models": False,
    },
    "pymupdf4llm": {
        "name": "PyMuPDF4LLM",
        "description": (
            "Lightweight PDF/e-book → Markdown built on PyMuPDF. No model downloads or CUDA, "
            "so it runs on low-end machines."
        ),
        "needs_local_models": False,
    },
    "markitdown": {
        "name": "markitdown",
        "description": (
            "Lightweight Markdown conversion with broad format support. No model downloads."
        ),
        "needs_local_models": False,
    },
    "docling": {
        "name": "Docling",
        "description": (
            "Structured conversion of PDF/Office/HTML/images. Downloads layout/table models on first run."
        ),
        "needs_local_models": True,
    },
    "mineru": {
        "name": "MinerU",
        "description": (
            "High-accuracy PDF parsing with OCR, formula, and table support. "
            "Runs locally or via the hosted API."
        ),
        "needs_local_models": True,
    },
}

# Nama paket pip yang benar-benar dipasang (bukan extra nalar-ai yang fiktif).
INSTALL_PACKAGES: dict[str, str] = {
    "pymupdf4llm": "pymupdf4llm",
    "markitdown": "markitdown",
    "docling": "docling",
    "mineru": "mineru[core]",
}

# Nama modul yang dicek dengan importlib untuk deteksi "terpasang".
_IMPORT_NAMES: dict[str, str] = {
    "pymupdf4llm": "pymupdf4llm",
    "markitdown": "markitdown",
    "docling": "docling",
    "mineru": "mineru",
}

# Opsi bawaan per engine; halaman membaca `data.engines.<id> || {}`.
DEFAULT_OPTIONS: dict[str, dict] = {
    "docling": {"do_ocr": False, "do_table_structure": True, "allow_local_model_download": False},
    "markitdown": {"enable_llm_image_description": False},
    "pymupdf4llm": {"write_images": True, "image_format": "png", "image_dpi": 150},
}

DEFAULT_MINERU_SETTINGS: dict = {
    "mode": "local",
    "api_base_url": "https://mineru.net",
    "local_cli_path": "",
    "model_download_source": "huggingface",
    "model_download_endpoint": "",
    "model_version": "pipeline",
    "language": "auto",
    "enable_formula": True,
    "enable_table": True,
    "is_ocr": False,
    "allow_local_model_download": False,
}

# Lokasi cache model docling yang diunduh `docling-tools models download`.
_DOCLING_MODELS_DIRS = (
    Path.home() / ".cache" / "docling" / "models",
    Path.home() / ".cache" / "docling" / "model_artifacts",
)


def is_installed(engine_id: str) -> bool:
    module = _IMPORT_NAMES.get(engine_id)
    if not module:
        return False
    if importlib.util.find_spec(module) is not None:
        return True
    # MinerU v1 memakai magic_pdf sebagai nama paketnya.
    if engine_id == "mineru" and importlib.util.find_spec("magic_pdf") is not None:
        return True
    return False


def docling_models_ready() -> bool:
    for path in _DOCLING_MODELS_DIRS:
        if path.is_dir() and any(path.iterdir()):
            return True
    return False


def find_mineru_cli(local_cli_path: str) -> dict:
    """Deteksi CLI mineru: PATH dulu, lalu path yang dikonfigurasi user."""
    configured = (local_cli_path or "").strip()
    on_path = shutil.which("mineru") or shutil.which("mineru-cli")
    if on_path:
        return {"found": True, "command": on_path, "path": on_path, "source": "path"}
    if configured and os.path.exists(configured):
        return {"found": True, "command": configured, "path": configured, "source": "configured"}
    if configured:
        return {"found": False, "command": configured, "path": configured, "source": "configured"}
    return {"found": False, "command": "", "path": "", "source": "path"}


async def get_or_create(db: AsyncSession, user_id: uuid.UUID) -> DocumentParsingSetting:
    row = await db.scalar(
        select(DocumentParsingSetting).where(DocumentParsingSetting.user_id == user_id)
    )
    if row is None:
        row = DocumentParsingSetting(user_id=user_id)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


def mineru_payload(row: DocumentParsingSetting) -> dict:
    """Blok ``mineru`` untuk payload halaman document-parsing."""
    state = row.load_mineru()
    settings = {**DEFAULT_MINERU_SETTINGS, **(state.get("settings") or {})}
    api_token_set = bool(state.get("api_token_encrypted"))
    return {
        "api_token_set": api_token_set,
        "local_cli": find_mineru_cli(settings.get("local_cli_path", "")),
    }


def readiness_for(engine_id: str, row: DocumentParsingSetting, mineru: dict | None = None) -> dict:
    """Readiness per engine: (ready, reason, message) sesuai kontrak frontend."""
    if engine_id == "text_only":
        return {"ready": True, "reason": "ready", "message": ""}
    if engine_id in ("pymupdf4llm", "markitdown"):
        if is_installed(engine_id):
            return {"ready": True, "reason": "ready", "message": ""}
        return {
            "ready": False,
            "reason": "not_installed",
            "message": "Package belum terpasang. Pasang lewat tombol di bawah.",
        }
    if engine_id == "docling":
        if not is_installed(engine_id):
            return {
                "ready": False,
                "reason": "not_installed",
                "message": "Package belum terpasang. Pasang lewat tombol di bawah.",
            }
        opts = row.load_options().get("docling") or DEFAULT_OPTIONS["docling"]
        if docling_models_ready() or opts.get("allow_local_model_download"):
            return {"ready": True, "reason": "ready", "message": ""}
        return {
            "ready": False,
            "reason": "models_missing",
            "message": (
                "Model layout/table belum diunduh. Unduh eksplisit di bawah, atau aktifkan "
                "unduhan otomatis agar parsing pertama menarik modelnya."
            ),
        }
    if engine_id == "mineru":
        if mineru is None:
            mineru = {"api_token_set": False, "local_cli": {"found": False}}
        if mineru.get("api_token_set"):
            return {"ready": True, "reason": "ready", "message": ""}
        if mineru.get("local_cli", {}).get("found"):
            return {"ready": True, "reason": "ready", "message": ""}
        return {
            "ready": False,
            "reason": "not_configured",
            "message": "Atur token API cloud atau pastikan CLI lokal tersedia di halaman MinerU.",
        }
    return {"ready": False, "reason": "unknown", "message": ""}


async def build_payload(db: AsyncSession, user: User) -> dict:
    row = await get_or_create(db, user.id)
    options = row.load_options()
    mineru_block = mineru_payload(row)

    engines: dict[str, dict] = {}
    for engine_id in ENGINE_IDS:
        engines[engine_id] = {
            **(DEFAULT_OPTIONS.get(engine_id) or {}),
            **(options.get(engine_id) or {}),
        }

    available_engines = [
        {
            "id": engine_id,
            "name": meta["name"],
            "description": meta["description"],
            "needs_local_models": meta["needs_local_models"],
            "available": is_installed(engine_id) or engine_id == "text_only",
        }
        for engine_id, meta in ENGINE_META.items()
    ]

    readiness = {
        engine_id: readiness_for(engine_id, row, mineru_block)
        for engine_id in ENGINE_IDS
    }

    installable = [
        engine_id
        for engine_id in INSTALL_PACKAGES
        if not is_installed(engine_id)
    ]

    return {
        "engine": row.engine,
        "engines": engines,
        "available_engines": available_engines,
        "readiness": readiness,
        "installable": installable,
        "mineru": mineru_block,
    }


# ── State MinerU (dipakai router /settings/mineru) ──────────────────────────

def mineru_settings_with_token(row: DocumentParsingSetting) -> tuple[dict, bool, str]:
    """Kembalikan (settings, api_token_set, token_plain)."""
    state = row.load_mineru()
    settings = {**DEFAULT_MINERU_SETTINGS, **(state.get("settings") or {})}
    encrypted = state.get("api_token_encrypted") or ""
    return settings, bool(encrypted), decrypt_api_key(encrypted)


def save_mineru_settings(row: DocumentParsingSetting, settings: dict, api_token: str | None) -> None:
    """Simpan setelan mineru; token None = biarkan, '' = hapus, lainnya = enkripsi baru."""
    state = row.load_mineru()
    stored_settings = {**DEFAULT_MINERU_SETTINGS, **(state.get("settings") or {}), **settings}
    # Jangan pernah menyimpan token ke dalam settings (field api_token tidak ada di sana).
    stored_settings.pop("api_token", None)
    state["settings"] = stored_settings
    if api_token is not None:
        token = api_token.strip()
        if token:
            state["api_token_encrypted"] = encrypt_api_key(token)
        else:
            state.pop("api_token_encrypted", None)
    row.save_mineru(state)


async def set_active_engine(db: AsyncSession, user: User, engine_id: str) -> None:
    """Set engine aktif di tabel document_parsing + sinkron ke user_preferences."""
    row = await get_or_create(db, user.id)
    row.engine = engine_id
    pref = await db.scalar(select(UserPreference).where(UserPreference.user_id == user.id))
    if pref is None:
        pref = UserPreference(user_id=user.id)
        db.add(pref)
    pref.document_parsing_engine = engine_id
    await db.commit()
    await db.refresh(row)


def merge_options(row: DocumentParsingSetting, patches: dict[str, dict]) -> None:
    """Gabungkan opsi per engine (patch parsial dari PUT)."""
    options = row.load_options()
    for engine_id, patch in patches.items():
        if engine_id not in DEFAULT_OPTIONS:
            continue
        current = {**DEFAULT_OPTIONS[engine_id], **(options.get(engine_id) or {})}
        current.update({k: v for k, v in patch.items() if k in DEFAULT_OPTIONS[engine_id]})
        options[engine_id] = current
    row.save_options(options)
