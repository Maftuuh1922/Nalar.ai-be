"""Schemas Pydantic untuk model Agent."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Nama agen AI, misal: 'Tutor Fisika'")
    role: str = Field(..., min_length=1, max_length=200, description="Deskripsi singkat peran agen")
    system_prompt: str = Field(..., min_length=10, description="Instruksi/prompt yang menentukan perilaku AI")
    avatar_icon: str = Field(default="Bot", max_length=50, description="Nama ikon Lucide (misal: Bot, BookOpen, Cpu)")


class AgentUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    role: str | None = Field(None, min_length=1, max_length=200)
    system_prompt: str | None = Field(None, min_length=10)
    avatar_icon: str | None = Field(None, max_length=50)


class AgentResponse(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    role: str
    system_prompt: str
    avatar_icon: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
