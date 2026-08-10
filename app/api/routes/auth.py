"""Endpoint autentikasi: registrasi, login, dan info user yang sedang login."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_optional_user
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, RegisterResponse, TokenResponse
from app.schemas.user import AuthStatusResponse, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])

TOKEN_COOKIE = "nalar_token"


def _set_token_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=TOKEN_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24,  # 24 hours
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    existing = await db.scalar(select(User).where(User.username == payload.username))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username sudah terdaftar. Silakan login.",
        )

    user_count = await db.scalar(select(func.count()).select_from(User))
    is_admin = user_count == 0

    user = User(
        username=payload.username,
        hashed_password=hash_password(payload.password),
        is_admin=is_admin,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(subject=str(user.id))
    _set_token_cookie(response, token)

    return RegisterResponse(
        ok=True,
        role="admin" if is_admin else "user",
        is_first_user=is_admin,
    )


@router.post("/login")
async def login(
    payload: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    user = await db.scalar(select(User).where(User.username == payload.username))
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Username atau kata sandi salah",
        )

    token = create_access_token(subject=str(user.id))
    _set_token_cookie(response, token)

    return {"ok": True}


@router.get("/status")
async def auth_status(
    current_user: User | None = Depends(get_optional_user),
) -> AuthStatusResponse:
    if current_user is None:
        return AuthStatusResponse(enabled=True, authenticated=False)
    return AuthStatusResponse(
        enabled=True,
        authenticated=True,
        user_id=str(current_user.id),
        username=current_user.username,
        role="admin" if current_user.is_admin else "user",
        is_admin=current_user.is_admin,
    )


@router.get("/is_first_user")
async def is_first_user(db: AsyncSession = Depends(get_db)) -> dict:
    user_count = await db.scalar(select(func.count()).select_from(User))
    return {"is_first_user": user_count == 0}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(TOKEN_COOKIE, httponly=True, samesite="lax")
    return {"ok": True}


@router.get("/me", response_model=UserResponse)
async def read_current_user(
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    return UserResponse.model_validate(current_user)
