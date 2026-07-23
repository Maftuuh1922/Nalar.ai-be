"""Schemas Pydantic untuk model Notebook."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class NotebookCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="Judul catatan")
    content: str = Field(default="", description="Isi teks (markdown/HTML)")


class NotebookUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    content: str | None = Field(None)


class DocxExportRequest(BaseModel):
    content: str
    title: str = Field("Document", min_length=1, max_length=255)


class NotebookResponse(BaseModel):
    id: UUID
    user_id: UUID
    title: str
    content: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
