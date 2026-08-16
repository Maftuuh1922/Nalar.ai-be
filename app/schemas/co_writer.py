"""Schemas Pydantic untuk fitur Co-Writer.

Catatan: frontend menghitung umur relatif dengan ``Date.now() / 1000 - seconds``,
jadi ``created_at`` / ``updated_at`` di response HARUS epoch detik (int), bukan
string ISO.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CoWriterCreate(BaseModel):
    title: str | None = Field(None, max_length=255, description="Judul draf; bila kosong diturunkan dari isi")
    content: str = Field("", description="Isi draf markdown")
    folder_id: UUID | None = Field(None, description="Folder tujuan; null = simpan di akar")


class CoWriterUpdate(BaseModel):
    title: str | None = Field(None, max_length=255, description="null = tidak diubah")
    content: str | None = Field(None, description="null = tidak diubah; string kosong = dikosongkan")


class CoWriterDocumentOut(BaseModel):
    id: UUID
    title: str
    content: str
    created_at: int
    updated_at: int
    source_format: str | None = Field(
        None,
        description="Format berkas sumber yang dipertahankan saat impor, mis. pdf atau docx",
    )
    content_format: str | None = Field(
        None,
        description="Format isi draf: 'latex' untuk draf baru, None untuk draf Markdown lama",
    )
    outline: dict | None = Field(None, description="Auto-extract outline saat import (PRD v2.4)")


class CoWriterSummaryOut(BaseModel):
    id: UUID
    title: str
    created_at: int
    updated_at: int
    preview: str
    folder_id: UUID | None = Field(None, description="Folder tempat draf disimpan; null = akar")


# ── Folder (pengelompokan draf, boleh bersarang) ────────────────────────────


class CoWriterFolderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    parent_id: UUID | None = Field(None, description="Folder induk; null = folder akar")
    color: str | None = Field(None, max_length=7, description='Warna aksen "#rrggbb"')


class CoWriterFolderUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255, description="null = tidak diubah")
    color: str | None = Field(None, max_length=7, description="null = tidak diubah")
    # Dibedakan dari "tidak dikirim" lewat model_fields_set: mengirim
    # "parent_id": null berarti pindahkan folder ke akar.
    parent_id: UUID | None = Field(None, description="Induk baru; kirim null untuk memindahkan ke akar")


class CoWriterFolderResponse(BaseModel):
    id: UUID
    name: str
    parent_id: UUID | None
    color: str | None
    document_count: int = Field(
        ...,
        description="Jumlah draf di folder ini DAN seluruh subfoldernya — sama dengan yang tampil saat folder dipilih",
    )
    created_at: int


class CoWriterFolderListOut(BaseModel):
    folders: list[CoWriterFolderResponse]


class CoWriterMoveRequest(BaseModel):
    folder_id: UUID | None = Field(None, description="Folder tujuan; null = keluarkan ke akar")


class CoWriterListOut(BaseModel):
    documents: list[CoWriterSummaryOut]


class CoWriterDeletedOut(BaseModel):
    deleted: bool


class CoWriterEditRequest(BaseModel):
    text: str = Field(..., description="Seluruh isi draf markdown")
    instruction: str = Field("", max_length=4000)
    action: Literal["rewrite", "shorten", "expand"] = "rewrite"
    source: str | None = Field(None, description='"rag" atau "web" untuk menambah konteks, atau null')
    kb_name: str | None = Field(None, description="Nama knowledge base saat source='rag'")


class CoWriterEditResponse(BaseModel):
    edited_text: str


class CoWriterAutoMarkRequest(BaseModel):
    text: str = Field(..., description="Draf markdown polos yang ingin diberi anotasi")


class CoWriterAutoMarkResponse(BaseModel):
    marked_text: str


class CoWriterStreamEditRequest(BaseModel):
    selected_text: str = Field(..., description="Teks terpilih yang diedit")
    instruction: str = Field("", max_length=4000, description="Instruksi bebas pengguna")
    mode: Literal["none", "shorten", "expand", "rewrite"] = "rewrite"
    tools: list[str] = Field(default_factory=list, description='Tool aktif, mis. ["rag", "web"]')
    kb_name: str | None = Field(None, description="Nama knowledge base saat tool 'rag' aktif")


# ── Agentic write & integrasi Learning Space ────────────────────────────────


class AgenticWriteRequest(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=4000, description="Perintah menulis, mis. 'tulis Bab 2 Tinjauan Pustaka'")
    group_id: UUID = Field(..., description="Grup laporan yang referensinya dipakai")
    format: str = Field("ieee", description="Format sitasi: ieee/apa/mla/chicago/harvard/vancouver/sni")
    use_rag: bool = Field(True, description="Pakai konteks isi jurnal (RAG) dari knowledge base")


class AgenticWriteResponse(BaseModel):
    draft: str = Field(..., description="Draf hasil tulisan AI — BELUM diterapkan; tunggu konfirmasi user")
    references: list[str] = Field(default_factory=list, description="Sitasi yang dipakai, urut kemunculan")
    citation_count: int = 0
    confirm_required: bool = True


class LearningSpaceData(BaseModel):
    """Data dari Learning Space yang bisa ditarik Co-Writer (tanpa upload ulang)."""
    groups: list[dict]
    references: list[dict]
    saved_citations: list[dict]
    chat_history: list[dict]
    drafts: list[dict]


class ImportChatRequest(BaseModel):
    doc_id: UUID | None = Field(None, description="Dokumen tujuan; None = buat dokumen baru")
    session_id: UUID = Field(..., description="Sesi chat yang diekspor")
