"""Endpoint setelan capability per user (GET/PUT /capabilities/settings)."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.services.capability_settings import (
    get_capability_settings,
    save_capability_settings,
)

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


@router.get("/settings")
async def get_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_capability_settings(db, current_user.id)


@router.put("/settings")
async def put_settings(
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await save_capability_settings(db, current_user.id, payload)
