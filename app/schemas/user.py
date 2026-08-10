"""Skema Pydantic untuk data user."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: str | None = None
    full_name: str | None = None
    role: str = "user"
    is_admin: bool = False
    created_at: datetime


class AuthStatusResponse(BaseModel):
    enabled: bool = True
    authenticated: bool = False
    user_id: str | None = None
    username: str | None = None
    role: str | None = None
    is_admin: bool = False
    avatar: str = ""
