"""Dependency FastAPI yang dipakai lintas endpoint (DB session, user login)."""

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User

# tokenUrl hanya dipakai untuk dokumentasi Swagger UI
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Kredensial tidak valid atau sudah kedaluwarsa",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if token is None:
        raise credentials_error

    subject = decode_access_token(token)
    if subject is None:
        raise credentials_error

    try:
        user_id = uuid.UUID(subject)
    except ValueError as exc:
        raise credentials_error from exc

    user = await db.get(User, user_id)
    if user is None:
        raise credentials_error

    return user
