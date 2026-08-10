"""Skema Pydantic untuk request/response autentikasi."""

from pydantic import BaseModel, Field

from app.schemas.user import UserResponse


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterResponse(BaseModel):
    ok: bool = True
    role: str = "user"
    is_first_user: bool = False


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
