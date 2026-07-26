"""Endpoint preferensi pengguna untuk tab-tab di modal Pengaturan."""

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.models.user_preference import UserPreference
from app.schemas.preference import PreferenceResponse, PreferenceUpdate
from app.services.preferences import get_preferences
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/preferences", tags=["preferences"])


@router.get("", response_model=PreferenceResponse)
async def read_preferences(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserPreference:
    """Ambil semua preferensi milik user yang login."""
    return await get_preferences(db, current_user.id)


@router.put("", response_model=PreferenceResponse)
async def update_preferences(
    payload: PreferenceUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserPreference:
    """Perbarui sebagian preferensi; field yang tidak dikirim dibiarkan apa adanya."""
    pref = await get_preferences(db, current_user.id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "proxy_url":
            value = (value or "").strip() or None
        elif field == "custom_instructions":
            value = (value or "").strip() or None
        setattr(pref, field, value)
    await db.commit()
    await db.refresh(pref)
    return pref
