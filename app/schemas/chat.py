"""Skema Pydantic untuk chat RAG."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    session_id: uuid.UUID | None = None
    document_ids: list[uuid.UUID] | None = None
    agent_id: uuid.UUID | None = None
    enable_reasoning: bool = False
    enable_rtk: bool = False


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
    sources_json: str | None = None
    created_at: datetime
