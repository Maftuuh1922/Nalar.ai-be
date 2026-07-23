"""Skema Pydantic untuk konfigurasi model AI."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelConfigRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="Nama konfigurasi (contoh: OpenAI)")
    base_url: str = Field(min_length=1, max_length=500, description="Base URL API (contoh: https://api.openai.com/v1)")
    api_key: str = Field(
        default="",
        max_length=500,
        description="API key. Boleh kosong saat memperbarui konfigurasi agar key lama dipertahankan.",
    )
    model_name: str = Field(min_length=1, max_length=200, description="Nama model LLM (contoh: gpt-4o-mini)")
    embedding_model: str = Field(min_length=1, max_length=200, description="Nama model embedding (contoh: text-embedding-3-small)")
    is_active: bool = Field(default=False, description="Tandai sebagai aktif")


class ModelConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    base_url: str
    model_name: str
    embedding_model: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
