"""Endpoint pengaturan model AI per user."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.encryption import encrypt_api_key
from app.db.session import get_db
from app.models.model_config import ModelConfig
from app.models.user import User
from app.schemas.model_config import ModelConfigRequest, ModelConfigResponse

router = APIRouter(prefix="/settings/model", tags=["settings"])


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
    )
    db.add(new_config)
    await db.commit()
    await db.refresh(new_config)
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
