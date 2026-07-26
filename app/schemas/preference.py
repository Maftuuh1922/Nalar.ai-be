"""Skema preferensi pengguna (tab-tab di modal Pengaturan)."""

from typing import Literal

from pydantic import BaseModel, Field


class PreferenceResponse(BaseModel):
    chat_temperature: float
    chat_max_tokens: int
    history_limit: int
    enable_web_tools: bool
    enable_document_tools: bool
    enable_suggestions: bool

    chunk_size: int
    chunk_overlap: int
    retrieval_top_k: int

    request_timeout: int
    proxy_url: str | None = None
    bypass_proxy_local: bool

    research_default_depth: str
    default_quiz_questions: int
    custom_instructions: str | None = None

    model_config = {"from_attributes": True}


class PreferenceUpdate(BaseModel):
    """Semua field opsional supaya tiap tab bisa menyimpan bagiannya sendiri."""

    chat_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    chat_max_tokens: int | None = Field(default=None, ge=256, le=64000)
    history_limit: int | None = Field(default=None, ge=0, le=100)
    enable_web_tools: bool | None = None
    enable_document_tools: bool | None = None
    enable_suggestions: bool | None = None

    chunk_size: int | None = Field(default=None, ge=128, le=4000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=1000)
    retrieval_top_k: int | None = Field(default=None, ge=1, le=30)

    request_timeout: int | None = Field(default=None, ge=10, le=900)
    proxy_url: str | None = Field(default=None, max_length=500)
    bypass_proxy_local: bool | None = None

    research_default_depth: Literal["ringkas", "standar", "mendalam"] | None = None
    default_quiz_questions: int | None = Field(default=None, ge=1, le=30)
    custom_instructions: str | None = Field(default=None, max_length=4000)
