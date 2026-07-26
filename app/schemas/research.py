"""Skema request/response untuk fitur Riset Mendalam."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ResearchCreate(BaseModel):
    topic: str = Field(..., min_length=3, max_length=500)
    # Arahan bebas dari user, misal "bandingkan dengan kondisi di Indonesia".
    instructions: str | None = Field(default=None, max_length=2000)
    # Menentukan berapa banyak kueri pencarian dan panjang tiap bagian.
    depth: Literal["ringkas", "standar", "mendalam"] = "standar"


class ResearchSummary(BaseModel):
    """Bentuk ringkas untuk daftar riset di halaman utama."""

    id: UUID
    topic: str
    depth: str
    status: str
    progress_step: str
    progress_percent: int
    word_count: int
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ResearchDetail(ResearchSummary):
    instructions: str | None = None
    outline: list[Any] | None = None
    sources: list[Any] | None = None
    content_markdown: str = ""


class ResearchToNotebook(BaseModel):
    """Judul opsional saat laporan dikirim ke menu Catatan."""

    title: str | None = Field(default=None, max_length=255)
