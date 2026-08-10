"""Dependency FastAPI yang dipakai lintas endpoint (DB session, user login)."""

import uuid

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User

TOKEN_COOKIE = "nalar_token"


async def _resolve_user(
    token: str | None = Cookie(default=None, alias=TOKEN_COOKIE),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    if token is None:
        return None
    subject = decode_access_token(token)
    if subject is None:
        return None
    try:
        user_id = uuid.UUID(subject)
    except ValueError:
        return None
    return await db.get(User, user_id)


async def get_optional_user(
    current_user: User | None = Depends(_resolve_user),
) -> User | None:
    return current_user


credentials_error = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Kredensial tidak valid atau sudah kedaluwarsa",
)


async def get_current_user(
    current_user: User | None = Depends(_resolve_user),
) -> User:
    if current_user is None:
        raise credentials_error
    return current_user
