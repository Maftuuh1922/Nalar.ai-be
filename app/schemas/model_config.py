"""Skema Pydantic untuk konfigurasi model AI."""

import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Kemampuan yang dikenali sistem. "text" selalu dianggap ada.
KNOWN_CAPABILITIES = ("text", "vision", "code", "audio", "reasoning", "tools", "embedding")


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
    capabilities: list[str] = Field(
        default_factory=lambda: ["text"],
        description='Kemampuan model, contoh ["text", "vision", "tools"]',
    )
    provider_type: str = Field(
        default="openai-compatible",
        max_length=50,
        description="Jenis penyedia: openai-compatible / google / anthropic / ollama",
    )
    context_window: int = Field(default=65536, ge=1024, le=10_000_000, description="Perkiraan jendela konteks (token)")

    @field_validator("capabilities")
    @classmethod
    def _clean_capabilities(cls, value: list[str]) -> list[str]:
        """Buang kemampuan tak dikenal dan pastikan 'text' selalu ada."""
        cleaned = [c for c in dict.fromkeys(value) if c in KNOWN_CAPABILITIES]
        if "text" not in cleaned:
            cleaned.insert(0, "text")
        return cleaned


class ModelConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    base_url: str
    model_name: str
    embedding_model: str
    is_active: bool
    capabilities: list[str] = Field(default_factory=lambda: ["text"])
    provider_type: str = "openai-compatible"
    context_window: int = 65536
    created_at: datetime
    updated_at: datetime

    @field_validator("capabilities", mode="before")
    @classmethod
    def _parse_capabilities(cls, value: Any) -> list[str]:
        """Kolom disimpan sebagai JSON string di database, ubah jadi list."""
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                return ["text"]
            return parsed if isinstance(parsed, list) else ["text"]
        if isinstance(value, list):
            return value
        return ["text"]


class DetectRequest(BaseModel):
    """Permintaan uji koneksi + deteksi kemampuan sebuah endpoint AI."""

    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=500)
    model_name: str = Field(default="", max_length=200)
    embedding_model: str = Field(default="", max_length=200)
    config_id: uuid.UUID | None = Field(
        default=None,
        description="Bila diisi dan api_key kosong, key tersimpan milik konfigurasi ini yang dipakai.",
    )


class ProbeResult(BaseModel):
    """Hasil satu langkah pemeriksaan endpoint."""

    name: str
    label: str
    status: str = Field(description="ok | warn | fail | skip")
    message: str = ""
    latency_ms: int | None = None


class DetectResponse(BaseModel):
    """Ringkasan hasil diagnosa endpoint AI."""

    reachable: bool
    capabilities: list[str]
    provider_type: str
    context_window: int
    available_models: list[str] = Field(default_factory=list)
    probes: list[ProbeResult] = Field(default_factory=list)
