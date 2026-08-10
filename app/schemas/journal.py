"""Skema Pydantic untuk fitur Referensi Jurnal & Sitasi."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ── Grup laporan ─────────────────────────────────────────────────────────────


class JournalGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class JournalGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class JournalGroupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    created_at: datetime
    reference_count: int = 0


# ── Referensi jurnal ─────────────────────────────────────────────────────────


class JournalReferenceUpdate(BaseModel):
    title: str | None = None
    authors: list[str] | None = None
    year: int | None = None
    journal_name: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    publisher: str | None = None
    abstract: str | None = None
    group_id: uuid.UUID | None = None


class JournalReferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    group_id: uuid.UUID
    filename: str
    title: str
    authors: list[str] | None = None
    year: int | None = None
    journal_name: str = ""
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    publisher: str | None = None
    abstract: str | None = None
    status: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Sitasi ───────────────────────────────────────────────────────────────────


class CitationRequest(BaseModel):
    format: str = "ieee"


class CitationResponse(BaseModel):
    reference_id: uuid.UUID
    format: str
    citation: str


class BibliographyRequest(BaseModel):
    format: str = "ieee"


class BibliographyResponse(BaseModel):
    group_id: uuid.UUID
    format: str
    citations: list[str]
    bibliography: str


# ── Sitasi tersimpan (Learning Space) ────────────────────────────────────────


class CitationCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class CitationCategoryUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class CitationCategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime
    citation_count: int = 0


class SavedCitationCreate(BaseModel):
    category_id: uuid.UUID
    reference_id: uuid.UUID | None = None
    format: str = "ieee"
    citation_text: str = Field(min_length=1)
    note: str | None = None


class SavedCitationUpdate(BaseModel):
    category_id: uuid.UUID | None = None
    note: str | None = None


class SavedCitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category_id: uuid.UUID
    reference_id: uuid.UUID | None = None
    format: str
    citation_text: str
    note: str | None = None
    created_at: datetime
