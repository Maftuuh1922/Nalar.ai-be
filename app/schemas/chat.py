"""Skema Pydantic untuk chat RAG."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LLMSelectionPayload(BaseModel):
    """Referensi model pilihan: id profil + id model dari katalog Pengaturan."""

    profile_id: str
    model_id: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500000)
    session_id: uuid.UUID | None = None
    notebook_id: uuid.UUID | None = None
    document_ids: list[uuid.UUID] | None = None
    agent_id: uuid.UUID | None = None
    enable_reasoning: bool = False
    enable_rtk: bool = False
    images: list[str] | None = None
    ephemeral: bool = False
    custom_system_instruction: str | None = None
    # Tanpa nilai ini, model yang dipakai adalah profil/model aktif di Pengaturan.
    llm_selection: LLMSelectionPayload | None = None


class Source(BaseModel):
    filename: str
    page: str | None = None
    excerpt: str


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    rtk_saved_tokens: int = 0


class ChatResponse(BaseModel):
    id: uuid.UUID
    answer: str
    thinking_process: str | None = None
    sources: list[Source]
    documents_read: list[str] | None = None
    usage: TokenUsage | None = None
    created_at: datetime


class ChatHistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID | None = None
    role: str
    content: str
    images_json: str | None = None
    sources_json: str | None = None
    usage_json: str | None = None
    created_at: datetime
