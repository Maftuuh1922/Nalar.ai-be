"""Penyimpanan katalog layanan AI & preferensi tampilan per user.

Dipisahkan dari lapisan route supaya bisa dipakai bersama oleh endpoint
Pengaturan dan oleh resolver model saat chat berjalan.
"""

import copy
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ui_setting import UiSetting

DEFAULT_UI = {
    "theme": "ascii",
    "language": "en",
    "code_block_theme": "github-dark",
    "code_block_show_line_numbers": True,
    "code_block_wrap_long_lines": False,
}

# Layanan yang dikenal oleh halaman Pengaturan di frontend.
SERVICE_NAMES = ("llm", "embedding", "search", "tts", "stt", "imagegen", "videogen")


def empty_service() -> dict:
    return {"active_profile_id": None, "active_model_id": None, "profiles": []}


DEFAULT_CATALOG = {
    "version": 1,
    "services": {name: empty_service() for name in SERVICE_NAMES},
}

DEFAULT_PROVIDERS = {
    "llm": [
        {"value": "openai", "label": "OpenAI"},
        {"value": "anthropic", "label": "Anthropic"},
        {"value": "gemini", "label": "Google Gemini"},
        {"value": "ollama", "label": "Ollama", "base_url": "http://127.0.0.1:11434/v1"},
        {"value": "vllm", "label": "vLLM / Custom API", "base_url": "http://localhost:8090/v1"},
    ],
    "embedding": [
        {"value": "openai", "label": "OpenAI"},
        {"value": "ollama", "label": "Ollama", "base_url": "http://127.0.0.1:11434/v1"},
        {"value": "vllm", "label": "vLLM / Custom API", "base_url": "http://localhost:8090/v1"},
    ],
    "search": [
        {"value": "brave", "label": "Brave Search"},
        {"value": "duckduckgo", "label": "DuckDuckGo"},
        {"value": "searxng", "label": "SearXNG"},
    ],
    "tts": [{"value": "openai", "label": "OpenAI"}],
    "stt": [{"value": "openai", "label": "OpenAI"}],
    "imagegen": [{"value": "openai", "label": "OpenAI"}],
    "videogen": [{"value": "openai", "label": "OpenAI"}],
}


def load_json(raw: str | None, fallback: dict) -> dict:
    """Baca JSON string dari DB; kembalikan salinan fallback bila tidak valid."""
    try:
        data = json.loads(raw or "")
    except (TypeError, ValueError):
        return copy.deepcopy(fallback)
    return data if isinstance(data, dict) else copy.deepcopy(fallback)


def _dedupe_profile_ids(service: dict) -> None:
    """Pastikan setiap profil dalam sebuah layanan punya ``id`` unik.

    Data warisan/impor bisa memuat beberapa profil dengan id yang sama
    (mis. semuanya ``"llm-prof"``). ``find_by_id`` hanya mengembalikan yang
    PERTAMA, sehingga model pada profil kembar tak pernah ter-resolve dan
    pemilihan model di kolom chat tampak "macet" (lihat model_selection.py).
    Di sini id kembar diberi akhiran deterministik ("-2", "-3", …) supaya
    stabil di tiap pembacaan. Bila ``active_profile_id`` jadi ambigu karena
    remap, arahkan ke profil yang benar-benar memuat ``active_model_id``.
    """
    profiles = service.get("profiles")
    if not isinstance(profiles, list):
        return
    used: set = set()
    remapped = False
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        pid = profile.get("id")
        if pid in used:
            n = 2
            new_id = f"{pid}-{n}"
            while new_id in used:
                n += 1
                new_id = f"{pid}-{n}"
            profile["id"] = new_id
            used.add(new_id)
            remapped = True
        else:
            used.add(pid)
    if not remapped:
        return
    active_model_id = service.get("active_model_id")
    if not active_model_id:
        return
    for profile in profiles:
        if isinstance(profile, dict) and any(
            isinstance(m, dict) and m.get("id") == active_model_id
            for m in (profile.get("models") or [])
        ):
            service["active_profile_id"] = profile.get("id")
            return


def merge_catalog(raw: object) -> dict:
    """Lengkapi katalog agar selalu memuat seluruh layanan yang dikenal FE.

    Frontend mengakses ``catalog.services.llm.active_profile_id`` tanpa
    pengecekan, jadi bentuk yang dikembalikan harus selalu utuh.
    """
    catalog = copy.deepcopy(DEFAULT_CATALOG)
    if not isinstance(raw, dict):
        return catalog

    version = raw.get("version")
    if isinstance(version, int):
        catalog["version"] = version

    services = raw.get("services")
    if not isinstance(services, dict):
        return catalog

    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        merged = empty_service()
        merged.update(service)
        profiles = merged.get("profiles")
        merged["profiles"] = (
            [p for p in profiles if isinstance(p, dict)] if isinstance(profiles, list) else []
        )
        _dedupe_profile_ids(merged)
        catalog["services"][name] = merged

    return catalog


async def get_or_create_row(db: AsyncSession, user_id) -> UiSetting:
    """Ambil baris ui_settings milik user, buat bila belum ada."""
    row = await db.scalar(select(UiSetting).where(UiSetting.user_id == user_id))
    if row is None:
        row = UiSetting(
            user_id=user_id,
            catalog_json=json.dumps(DEFAULT_CATALOG),
            ui_json=json.dumps(DEFAULT_UI),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def read_catalog(db: AsyncSession, user_id) -> dict:
    row = await get_or_create_row(db, user_id)
    return merge_catalog(load_json(row.catalog_json, DEFAULT_CATALOG))


async def write_catalog(db: AsyncSession, user_id, catalog: object) -> dict:
    """Simpan katalog ke DB dan kembalikan bentuk yang tersimpan."""
    merged = merge_catalog(catalog)
    row = await get_or_create_row(db, user_id)
    row.catalog_json = json.dumps(merged)
    await db.commit()
    return merged


def find_by_id(items: object, wanted: object) -> dict | None:
    if not isinstance(items, list) or not wanted:
        return None
    for item in items:
        if isinstance(item, dict) and item.get("id") == wanted:
            return item
    return None


def to_int(value: object, fallback: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def active_profile_and_model(catalog: dict, service_name: str) -> tuple[dict | None, dict | None]:
    """Kembalikan (profil aktif, model aktif) untuk sebuah layanan.

    Bila model aktif tidak ditemukan tapi profilnya punya model, model pertama
    dipakai supaya menyimpan profil baru tanpa memilih model tidak berakhir
    dengan konfigurasi yang tidak terpakai sama sekali.
    """
    service = catalog.get("services", {}).get(service_name) or {}
    profile = find_by_id(service.get("profiles"), service.get("active_profile_id"))
    if profile is None:
        return None, None
    model = find_by_id(profile.get("models"), service.get("active_model_id"))
    if model is None:
        models = [
            m
            for m in profile.get("models", [])
            if isinstance(m, dict) and (m.get("model") or "").strip()
        ]
        model = models[0] if models else None
    return profile, model


__all__ = [
    "DEFAULT_CATALOG",
    "DEFAULT_PROVIDERS",
    "DEFAULT_UI",
    "SERVICE_NAMES",
    "active_profile_and_model",
    "empty_service",
    "find_by_id",
    "get_or_create_row",
    "load_json",
    "merge_catalog",
    "read_catalog",
    "to_int",
    "write_catalog",
]
