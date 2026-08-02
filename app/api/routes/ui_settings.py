"""Endpoint pengaturan UI dan katalog layanan AI.

Katalog (profil + daftar model per layanan) dan preferensi tampilan disimpan
per user di tabel ``ui_settings`` pada nalar_ai.db. Profil/model LLM yang aktif
juga dicerminkan ke tabel ``model_configs`` karena alur chat, indexing dokumen,
kuis, dan Riset Mendalam membaca konfigurasi aktif dari sana.
"""

import copy
import json

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.encryption import encrypt_api_key
from app.models.model_config import ModelConfig
from app.models.user import User
from app.services.model_probe import (
    capabilities_from_name,
    guess_context_window,
    guess_provider_type,
)
from app.services.model_selection import (
    VALID_CAPABILITY_TIERS,
    normalize_capability_tier,
    normalize_provider_type,
)
from app.services.ui_catalog import (
    DEFAULT_CATALOG,
    DEFAULT_PROVIDERS,
    DEFAULT_UI,
    active_profile_and_model,
    get_or_create_row,
    load_json,
    merge_catalog,
    read_catalog,
    to_int,
    write_catalog,
)

router = APIRouter(tags=["settings"])


def _require_catalog(body: object) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("catalog"), dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Body harus berisi objek 'catalog'.",
        )
    return body["catalog"]


async def _sync_active_model_config(db: AsyncSession, user: User, catalog: dict) -> bool:
    """Cerminkan profil/model LLM aktif ke tabel model_configs.

    Alur chat memilih ``ModelConfig`` dengan ``is_active=True``, jadi tanpa
    langkah ini model yang dipilih di Pengaturan tidak pernah dipakai.
    """
    profile, model = active_profile_and_model(catalog, "llm")
    if profile is None or model is None:
        return False

    model_name = (model.get("model") or "").strip()
    if not model_name:
        return False

    base_url = (profile.get("base_url") or "").strip()
    api_key = profile.get("api_key") or ""

    cfg = await db.scalar(
        select(ModelConfig).where(
            ModelConfig.user_id == user.id,
            ModelConfig.is_active == True,  # noqa: E712 — perbandingan kolom SQLAlchemy
        )
    )
    if cfg is None:
        # api_key_encrypted NOT NULL: selalu isi, termasuk untuk endpoint lokal
        # tanpa API key. Tanpa ini INSERT gagal dan model tidak pernah tersimpan.
        cfg = ModelConfig(
            user_id=user.id,
            is_active=True,
            api_key_encrypted=encrypt_api_key(""),
        )
        db.add(cfg)

    cfg.name = ((profile.get("name") or "").strip() or "Konfigurasi AI")[:100]
    cfg.base_url = base_url
    if api_key:
        cfg.api_key_encrypted = encrypt_api_key(api_key)
    elif not cfg.api_key_encrypted:
        cfg.api_key_encrypted = encrypt_api_key("")
    cfg.model_name = model_name

    _, embedding_model = active_profile_and_model(catalog, "embedding")
    embedding_name = (embedding_model or {}).get("model") or ""
    cfg.embedding_model = embedding_name.strip() or cfg.embedding_model or "text-embedding-ada-002"

    cfg.provider_type = guess_provider_type(base_url)
    cfg.capabilities = json.dumps(sorted(capabilities_from_name(model_name)))
    cfg.context_window = to_int(model.get("context_window"), guess_context_window(model_name))
    # Tier hanya boleh diisi oleh hasil verifikasi di /settings/model/detect.
    cfg.capability_tier = normalize_capability_tier(cfg.capability_tier)

    await db.commit()
    return True


# ─── Endpoint ──────────────────────────────────────────────────────────────


@router.get("/settings")
async def get_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await get_or_create_row(db, current_user.id)
    ui = dict(DEFAULT_UI)
    ui.update(load_json(row.ui_json, {}))
    return {
        "ui": ui,
        "catalog": merge_catalog(load_json(row.catalog_json, DEFAULT_CATALOG)),
        "providers": copy.deepcopy(DEFAULT_PROVIDERS),
    }


@router.put("/settings/ui")
async def update_ui_settings(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Simpan preferensi tampilan. Menerima patch sebagian, bukan objek penuh."""
    row = await get_or_create_row(db, current_user.id)
    ui = dict(DEFAULT_UI)
    ui.update(load_json(row.ui_json, {}))

    patch = body.get("ui") if isinstance(body.get("ui"), dict) else body
    ui.update({key: value for key, value in patch.items() if key in DEFAULT_UI})

    row.ui_json = json.dumps(ui)
    await db.commit()
    return {"ok": True, "ui": ui}


@router.get("/settings/catalog")
async def get_catalog(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await read_catalog(db, current_user.id)


@router.put("/settings/catalog")
async def update_catalog(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Simpan draf katalog tanpa menerapkannya ke konfigurasi model aktif."""
    saved = await write_catalog(db, current_user.id, _require_catalog(body))
    return {"ok": True, "catalog": saved}


@router.post("/settings/apply")
async def apply_settings(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Simpan katalog lalu terapkan profil/model LLM aktif ke model_configs."""
    saved = await write_catalog(db, current_user.id, _require_catalog(body))
    synced = await _sync_active_model_config(db, current_user, saved)
    return {
        "ok": True,
        "message": "Settings applied",
        "catalog": saved,
        "model_config_synced": synced,
    }


@router.post("/settings/tests/{service}/start")
async def start_service_test(
    service: str,
    body: dict,
    current_user: User = Depends(get_current_user),
):
    import uuid

    run_id = str(uuid.uuid4())
    return {"run_id": run_id, "detail": "Test started (mock)"}


@router.get("/settings/tests/{service}/{run_id}/events")
async def get_test_events(
    service: str,
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    async def event_generator():
        yield f"data: {json.dumps({'type': 'info', 'message': f'Starting mock test for {service}...'})}\n\n"
        import asyncio

        await asyncio.sleep(0.5)
        yield f"data: {json.dumps({'type': 'completed', 'message': f'{service} test completed successfully.'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _llm_options_from_model_configs(db: AsyncSession, user_id) -> dict:
    """Sumber cadangan: baca dari model_configs bila katalog masih kosong."""
    configs = list(await db.scalars(select(ModelConfig).where(ModelConfig.user_id == user_id)))
    options = []
    active = None
    for cfg in configs:
        options.append(
            {
                "profile_id": str(cfg.id),
                "model_id": cfg.model_name,
                "profile_name": cfg.name or cfg.model_name,
                "model_name": cfg.model_name,
                "model": cfg.model_name,
                "provider": normalize_provider_type(cfg.provider_type, cfg.base_url or ""),
                "context_window": cfg.context_window,
                "is_active_default": cfg.is_active,
            }
        )
        if cfg.is_active:
            active = {"profile_id": str(cfg.id), "model_id": cfg.model_name}
    return {"active": active, "options": options}


@router.get("/settings/llm-options")
async def get_llm_options(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Daftar model LLM untuk pemilih model di kolom chat.

    Dibangun dari katalog supaya setiap model yang ditambahkan di Pengaturan
    ikut muncul, bukan hanya satu baris konfigurasi aktif di model_configs.
    """
    catalog = await read_catalog(db, current_user.id)
    llm = catalog["services"].get("llm") or {}
    active_profile_id = llm.get("active_profile_id")
    active_model_id = llm.get("active_model_id")

    options: list[dict] = []
    active: dict | None = None

    for profile in llm.get("profiles", []):
        base_url = profile.get("base_url") or ""
        provider = profile.get("binding") or profile.get("provider") or guess_provider_type(base_url)
        profile_id = profile.get("id")
        profile_name = (profile.get("name") or "").strip()

        for model in profile.get("models", []):
            if not isinstance(model, dict):
                continue
            model_name = (model.get("model") or "").strip()
            model_id = model.get("id")
            if not model_name or not model_id or not profile_id:
                continue

            is_default = profile_id == active_profile_id and model_id == active_model_id
            options.append(
                {
                    "profile_id": profile_id,
                    "model_id": model_id,
                    "profile_name": profile_name or model_name,
                    "model_name": (model.get("name") or "").strip() or model_name,
                    "model": model_name,
                    "provider": provider,
                    "context_window": to_int(model.get("context_window"), guess_context_window(model_name)),
                    "is_active_default": is_default,
                }
            )
            if is_default:
                active = {"profile_id": profile_id, "model_id": model_id}

    if not options:
        return await _llm_options_from_model_configs(db, current_user.id)

    return {"active": active, "options": options}


@router.get("/settings/chat-attachments")
async def get_chat_attachments(
    current_user: User = Depends(get_current_user),
):
    return {}


@router.get("/settings/network")
async def get_network_settings(
    current_user: User = Depends(get_current_user),
):
    return {
        "settings": {
            "backend_port": 8087,
            "frontend_port": 3000,
            "public_api_base": "",
            "cors_origins": ["http://localhost:3000"],
        },
        "effective": {
            "backend_url": "http://127.0.0.1:8087",
            "frontend_url": "http://localhost:3000",
            "browser_api_base": "http://localhost:3000/api",
            "api_base_source": "default",
            "cors_mode": "permissive",
            "cors_origins": ["http://localhost:3000"],
            "allow_remote_http_origins": True,
        },
        "auth": {
            "enabled": False,
            "cookie_secure": False,
            "cookie_samesite": "lax",
            "cross_site_cookie_ready": False,
        },
        "restart_required": False,
    }


@router.put("/settings/network")
async def update_network_settings(
    body: dict,
    current_user: User = Depends(get_current_user),
):
    return {"ok": True}


@router.put("/settings/chat-response-timeout")
async def update_chat_response_timeout(
    body: dict,
    current_user: User = Depends(get_current_user),
):
    return {"ok": True}


@router.get("/subagents/settings")
async def get_subagents_settings(
    current_user: User = Depends(get_current_user),
):
    return {"enabled": False, "config": {}}


@router.get("/subagents/connections")
async def get_subagents_connections(
    current_user: User = Depends(get_current_user),
):
    return {"connections": []}


@router.get("/tools")
async def get_tools(
    current_user: User = Depends(get_current_user),
):
    return {"tools": []}
