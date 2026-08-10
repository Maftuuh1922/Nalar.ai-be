"""Endpoint pengaturan model AI per user."""

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.encryption import decrypt_api_key, encrypt_api_key
from app.db.session import get_db
from app.models.model_config import ModelConfig
from app.models.user import User
from app.schemas.model_config import (
    DetectRequest,
    DetectResponse,
    ModelConfigRequest,
    ModelConfigResponse,
)
from app.services.model_probe import probe_endpoint
from app.services.ui_catalog import find_by_id, read_catalog, write_catalog

router = APIRouter(prefix="/settings/model", tags=["settings"])


async def _sync_created_config_to_chat_catalog(
    db: AsyncSession,
    user: User,
    config: ModelConfig,
    payload: ModelConfigRequest,
) -> None:
    """Expose a newly created provider in the chat model selector."""
    catalog = await read_catalog(db, user.id)
    llm = catalog["services"]["llm"]
    profile_id = str(config.id)
    model_id = f"{profile_id}:model"
    profile = find_by_id(llm.get("profiles"), profile_id)

    if profile is None:
        profile = {
            "id": profile_id,
            "name": config.name,
            "binding": payload.provider_type,
            "provider": payload.provider_type,
            "base_url": config.base_url,
            "api_key": payload.api_key,
            "api_version": "",
            "extra_headers": {},
            "proxy": "",
            "models": [],
        }
        llm.setdefault("profiles", []).append(profile)
    else:
        profile["name"] = config.name
        profile["binding"] = payload.provider_type
        profile["provider"] = payload.provider_type
        profile["base_url"] = config.base_url
        if payload.api_key:
            profile["api_key"] = payload.api_key

    models = profile.setdefault("models", [])
    model = find_by_id(models, model_id)
    if model is None:
        models.append(
            {
                "id": model_id,
                "name": config.model_name,
                "model": config.model_name,
                "context_window": str(config.context_window),
            }
        )
    else:
        model["name"] = config.model_name
        model["model"] = config.model_name
        model["context_window"] = str(config.context_window)

    if config.is_active or not llm.get("active_profile_id"):
        llm["active_profile_id"] = profile_id
        llm["active_model_id"] = model_id

    await write_catalog(db, user.id, catalog)


@router.get("", response_model=list[ModelConfigResponse])
async def get_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ModelConfig]:
    """Ambil daftar konfigurasi model AI milik user."""
    result = await db.scalars(
        select(ModelConfig)
        .where(ModelConfig.user_id == current_user.id)
        .order_by(ModelConfig.created_at.asc())
    )
    return list(result.all())


@router.post("", response_model=ModelConfigResponse)
async def create_setting(
    payload: ModelConfigRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelConfig:
    """Buat konfigurasi model AI baru."""
    # API key bisa kosong untuk model lokal
    is_active = payload.is_active
    # Cek apakah ini konfigurasi pertama user
    from sqlalchemy import func
    count = await db.scalar(select(func.count(ModelConfig.id)).where(ModelConfig.user_id == current_user.id))

    is_active = payload.is_active
    if count == 0:
        is_active = True  # Otomatis jadi aktif jika ini yang pertama
        
    if is_active:
        # Nonaktifkan yang lain
        await db.execute(
            update(ModelConfig)
            .where(ModelConfig.user_id == current_user.id)
            .values(is_active=False)
        )

    new_config = ModelConfig(
        user_id=current_user.id,
        name=payload.name,
        base_url=payload.base_url,
        api_key_encrypted=encrypt_api_key(payload.api_key),
        model_name=payload.model_name,
        embedding_model=payload.embedding_model,
        is_active=is_active,
        capabilities=json.dumps(payload.capabilities),
        provider_type=payload.provider_type,
        capability_tier=payload.capability_tier,
        context_window=payload.context_window,
    )
    db.add(new_config)
    await db.commit()
    await db.refresh(new_config)
    await _sync_created_config_to_chat_catalog(db, current_user, new_config, payload)
    return new_config


@router.put("/{id}", response_model=ModelConfigResponse)
async def update_setting(
    id: uuid.UUID,
    payload: ModelConfigRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelConfig:
    """Perbarui konfigurasi model AI."""
    existing = await db.scalar(
        select(ModelConfig).where(ModelConfig.id == id, ModelConfig.user_id == current_user.id)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Konfigurasi tidak ditemukan")

    existing.name = payload.name
    existing.base_url = payload.base_url
    if payload.api_key:
        existing.api_key_encrypted = encrypt_api_key(payload.api_key)
    existing.model_name = payload.model_name
    existing.embedding_model = payload.embedding_model
    existing.capabilities = json.dumps(payload.capabilities)
    existing.provider_type = payload.provider_type
    existing.capability_tier = payload.capability_tier
    existing.context_window = payload.context_window
    existing.updated_at = datetime.now(timezone.utc)
    
    if payload.is_active and not existing.is_active:
        # Nonaktifkan yang lain
        await db.execute(
            update(ModelConfig)
            .where(ModelConfig.user_id == current_user.id, ModelConfig.id != id)
            .values(is_active=False)
        )
        existing.is_active = True

    await db.commit()
    await db.refresh(existing)
    return existing


@router.put("/{id}/active", response_model=ModelConfigResponse)
async def set_active_setting(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelConfig:
    """Tandai sebuah konfigurasi model sebagai aktif."""
    existing = await db.scalar(
        select(ModelConfig).where(ModelConfig.id == id, ModelConfig.user_id == current_user.id)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Konfigurasi tidak ditemukan")

    # Nonaktifkan yang lain
    await db.execute(
        update(ModelConfig)
        .where(ModelConfig.user_id == current_user.id, ModelConfig.id != id)
        .values(is_active=False)
    )
    
    existing.is_active = True
    existing.updated_at = datetime.now(timezone.utc)
    
    await db.commit()
    await db.refresh(existing)
    return existing


@router.post("/detect", response_model=DetectResponse)
async def detect_capabilities(
    payload: DetectRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DetectResponse:
    """Uji koneksi ke endpoint AI dan simpulkan kemampuannya.

    Pemeriksaan dilakukan sungguhan (daftar model, chat kecil, tool call,
    gambar 1x1 piksel, embedding) supaya hasilnya bisa dipercaya, bukan
    sekadar tebakan dari nama model.
    """
    api_key = payload.api_key
    if not api_key and payload.config_id:
        # Form pengaturan tidak pernah mengirim ulang key yang sudah tersimpan.
        existing = await db.scalar(
            select(ModelConfig).where(
                ModelConfig.id == payload.config_id,
                ModelConfig.user_id == current_user.id,
            )
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Konfigurasi tidak ditemukan")
        api_key = decrypt_api_key(existing.api_key_encrypted)

    result = await probe_endpoint(
        base_url=payload.base_url,
        api_key=api_key,
        model_name=payload.model_name,
        embedding_model=payload.embedding_model,
    )
    return DetectResponse(**result)


@router.delete("/{id}")
async def delete_setting(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Hapus konfigurasi model AI."""
    existing = await db.scalar(
        select(ModelConfig).where(ModelConfig.id == id, ModelConfig.user_id == current_user.id)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Konfigurasi tidak ditemukan")

    await db.delete(existing)
    await db.commit()
    return {"status": "ok"}
