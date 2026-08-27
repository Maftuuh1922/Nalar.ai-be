"""Endpoint fitur Referensi Jurnal & Sitasi.

Mengikuti pola ``research.py``: ownership via ``get_current_user``, 404 dengan
pesan Indonesia, background task untuk ekstraksi metadata.

Sub-router:
- /journal/groups          — CRUD grup laporan ("Laporan A/B/C")
- /journal/references      — upload PDF, edit metadata, generate sitasi
- /journal/citation-categories — CRUD kategori sitasi
- /journal/citations       — sitasi tersimpan per kategori
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.models.journal import CitationCategory, JournalGroup, JournalReference, SavedCitation
from app.models.user import User
from app.schemas.journal import (
    BibliographyRequest,
    BibliographyResponse,
    CitationCategoryCreate,
    CitationCategoryResponse,
    CitationCategoryUpdate,
    CitationRequest,
    CitationResponse,
    JournalGroupCreate,
    JournalGroupResponse,
    JournalGroupUpdate,
    JournalReferenceResponse,
    JournalReferenceUpdate,
    SavedCitationCreate,
    SavedCitationResponse,
    SavedCitationUpdate,
)
from app.services.citation_formatter import (
    CITATION_FORMATS,
    FORMAT_LABELS,
    CitationError,
    citation_meta_from_reference,
    generate_citation,
)
from app.services.journal_metadata import run_extract_metadata
from app.services.model_selection import ModelSelectionError, resolve_llm

router = APIRouter(prefix="/journal", tags=["journal"])

# File upload: hanya PDF (fase awal); batas ukuran mengikuti konfigurasi umum.
_ALLOWED_EXTENSIONS = {".pdf"}
_MAX_UPLOAD_MB = 25


def _upload_dir() -> str:
    base = getattr(settings, "UPLOAD_DIR", None) or os.path.join(os.getcwd(), "uploads")
    path = os.path.join(base, "journal_references")
    os.makedirs(path, exist_ok=True)
    return path


def _safe_filename(filename: str) -> str:
    name = os.path.basename(filename or "jurnal.pdf")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:200] or "jurnal.pdf"


# ── Helper ownership ─────────────────────────────────────────────────────────


async def _get_owned_group(db: AsyncSession, group_id: uuid.UUID, user: User) -> JournalGroup:
    group = await db.scalar(
        select(JournalGroup).where(
            JournalGroup.id == group_id, JournalGroup.user_id == user.id
        )
    )
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grup laporan tidak ditemukan.")
    return group


async def _default_group_id(db: AsyncSession, user: User) -> uuid.UUID:
    """Grup laporan pertama milik user; buat 'Referensi Chat' bila belum ada."""
    group = await db.scalar(
        select(JournalGroup)
        .where(JournalGroup.user_id == user.id)
        .order_by(JournalGroup.created_at.asc())
        .limit(1)
    )
    if group is not None:
        return group.id
    created = JournalGroup(user_id=user.id, name="Referensi Chat")
    db.add(created)
    await db.commit()
    await db.refresh(created)
    return created.id


async def _get_owned_reference(db: AsyncSession, reference_id: uuid.UUID, user: User) -> JournalReference:
    reference = await db.scalar(
        select(JournalReference).where(
            JournalReference.id == reference_id, JournalReference.user_id == user.id
        )
    )
    if reference is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Referensi jurnal tidak ditemukan.")
    return reference


async def _get_owned_category(db: AsyncSession, category_id: uuid.UUID, user: User) -> CitationCategory:
    category = await db.scalar(
        select(CitationCategory).where(
            CitationCategory.id == category_id, CitationCategory.user_id == user.id
        )
    )
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Kategori sitasi tidak ditemukan.")
    return category


async def _get_owned_saved_citation(db: AsyncSession, citation_id: uuid.UUID, user: User) -> SavedCitation:
    citation = await db.scalar(
        select(SavedCitation).where(
            SavedCitation.id == citation_id, SavedCitation.user_id == user.id
        )
    )
    if citation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sitasi tersimpan tidak ditemukan.")
    return citation


def _group_response(group: JournalGroup, count: int) -> JournalGroupResponse:
    return JournalGroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        created_at=group.created_at,
        reference_count=count,
    )


def _category_response(category: CitationCategory, count: int) -> CitationCategoryResponse:
    return CitationCategoryResponse(
        id=category.id,
        name=category.name,
        created_at=category.created_at,
        citation_count=count,
    )


# ── Grup laporan ─────────────────────────────────────────────────────────────


@router.get("/groups", response_model=list[JournalGroupResponse])
async def list_groups(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[JournalGroupResponse]:
    """Daftar grup laporan milik user, dengan jumlah referensi tiap grup."""
    groups = (
        await db.scalars(
            select(JournalGroup)
            .where(JournalGroup.user_id == current_user.id)
            .order_by(JournalGroup.created_at.asc())
        )
    ).all()
    counts = dict(
        (
            await db.execute(
                select(JournalReference.group_id, func.count())
                .where(JournalReference.user_id == current_user.id)
                .group_by(JournalReference.group_id)
            )
        ).all()
    )
    return [_group_response(g, counts.get(g.id, 0)) for g in groups]


@router.post("/groups", response_model=JournalGroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: JournalGroupCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JournalGroupResponse:
    """Buat grup laporan baru (mis. 'Laporan A')."""
    group = JournalGroup(
        user_id=current_user.id,
        name=payload.name.strip(),
        description=(payload.description or "").strip() or None,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return _group_response(group, 0)


@router.put("/groups/{group_id}", response_model=JournalGroupResponse)
async def update_group(
    group_id: uuid.UUID,
    payload: JournalGroupUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JournalGroupResponse:
    """Ubah nama/deskripsi grup laporan."""
    group = await _get_owned_group(db, group_id, current_user)
    if payload.name is not None and payload.name.strip():
        group.name = payload.name.strip()
    if payload.description is not None:
        group.description = payload.description.strip() or None
    await db.commit()
    await db.refresh(group)
    count = await db.scalar(
        select(func.count()).select_from(JournalReference).where(
            JournalReference.group_id == group.id, JournalReference.user_id == current_user.id
        )
    )
    return _group_response(group, count or 0)


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hapus grup beserta semua referensi di dalamnya (CASCADE) + file PDF."""
    group = await _get_owned_group(db, group_id, current_user)
    refs = (
        await db.scalars(
            select(JournalReference).where(
                JournalReference.group_id == group.id, JournalReference.user_id == current_user.id
            )
        )
    ).all()
    for ref in refs:
        if ref.file_path and os.path.exists(ref.file_path):
            try:
                os.remove(ref.file_path)
            except OSError:
                pass
    await db.delete(group)
    await db.commit()


# ── Referensi jurnal ─────────────────────────────────────────────────────────


@router.get("/references", response_model=list[JournalReferenceResponse])
async def list_references(
    group_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[JournalReference]:
    """Daftar referensi; filter grup bila ``group_id`` diberikan."""
    stmt = (
        select(JournalReference)
        .where(JournalReference.user_id == current_user.id)
        .order_by(JournalReference.created_at.desc())
    )
    if group_id is not None:
        stmt = stmt.where(JournalReference.group_id == group_id)
    result = await db.scalars(stmt)
    return list(result.all())


@router.post("/references", response_model=JournalReferenceResponse, status_code=status.HTTP_201_CREATED)
async def create_reference(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    group_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
) -> JournalReferenceResponse:
    """Upload PDF jurnal → simpan file → ekstraksi metadata di background."""
    await _get_owned_group(db, group_id, current_user)

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Format file tidak didukung: {ext or '(tanpa ekstensi)'}. Gunakan PDF.",
        )

    # Baca isi file untuk cek ukuran & simpan.
    contents = await file.read()
    if len(contents) > _MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File terlalu besar (maks {_MAX_UPLOAD_MB} MB).",
        )
    if not contents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File kosong.")

    safe_name = _safe_filename(file.filename)
    stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    target_dir = _upload_dir()
    target_path = os.path.join(target_dir, stored_name)
    with open(target_path, "wb") as out:
        out.write(contents)

    reference = JournalReference(
        user_id=current_user.id,
        group_id=group_id,
        filename=safe_name,
        file_path=target_path,
        title=safe_name,  # sementara; diganti hasil ekstraksi
        status="pending",
    )
    db.add(reference)
    await db.commit()
    await db.refresh(reference)

    # Ekstraksi metadata di background memakai konfigurasi LLM user.
    try:
        llm = await resolve_llm(db, current_user.id)
        background_tasks.add_task(
            run_extract_metadata,
            reference_id=str(reference.id),
            base_url=llm.base_url,
            api_key=llm.api_key or "dummy",
            model_name=llm.model_name,
            db_url=settings.DATABASE_URL,
        )
    except ModelSelectionError:
        # Tanpa model aktif: file tersimpan, metadata bisa diisi manual.
        reference.status = "pending"
        reference.error_message = "Model LLM belum dikonfigurasi — isi metadata secara manual."
        await db.commit()
        await db.refresh(reference)

    return reference


@router.put("/references/{reference_id}", response_model=JournalReferenceResponse)
async def update_reference(
    reference_id: uuid.UUID,
    payload: JournalReferenceUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JournalReferenceResponse:
    """Edit metadata manual (hasil AI tidak selalu benar) / pindah grup."""
    reference = await _get_owned_reference(db, reference_id, current_user)
    updates = payload.model_dump(exclude_unset=True)
    if "group_id" in updates and updates["group_id"] is not None:
        await _get_owned_group(db, updates["group_id"], current_user)
    for field, value in updates.items():
        setattr(reference, field, value)
    await db.commit()
    await db.refresh(reference)
    return reference


@router.delete("/references/{reference_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_reference(
    reference_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hapus referensi beserta file PDF-nya."""
    reference = await _get_owned_reference(db, reference_id, current_user)
    if reference.file_path and os.path.exists(reference.file_path):
        try:
            os.remove(reference.file_path)
        except OSError:
            pass
    await db.delete(reference)
    await db.commit()


# ── Generate sitasi ──────────────────────────────────────────────────────────


@router.post("/references/{reference_id}/citation", response_model=CitationResponse)
async def generate_reference_citation(
    reference_id: uuid.UUID,
    payload: CitationRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CitationResponse:
    """Generate sitasi (IEEE/APA/MLA/Chicago/Harvard/Vancouver/SNI) dari metadata."""
    reference = await _get_owned_reference(db, reference_id, current_user)
    try:
        citation = generate_citation(citation_meta_from_reference(reference), payload.format)
    except CitationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return CitationResponse(reference_id=reference.id, format=payload.format, citation=citation)


@router.post("/groups/{group_id}/bibliography", response_model=BibliographyResponse)
async def generate_group_bibliography(
    group_id: uuid.UUID,
    payload: BibliographyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BibliographyResponse:
    """Daftar pustaka lengkap untuk satu grup laporan.

    IEEE/Vancouver: urut sesuai kemunculan [1]..[n] (created_at asc).
    APA/Harvard/MLA/Chicago/SNI: urut alfabetis penulis.
    """
    await _get_owned_group(db, group_id, current_user)
    refs = (
        await db.scalars(
            select(JournalReference)
            .where(
                JournalReference.group_id == group_id,
                JournalReference.user_id == current_user.id,
            )
            .order_by(JournalReference.created_at.asc())
        )
    ).all()

    citations: list[str] = []
    for ref in refs:
        try:
            citations.append(generate_citation(citation_meta_from_reference(ref), payload.format))
        except CitationError:
            # Referensi dengan metadata belum lengkap dilewati, jangan gagalkan semua.
            continue

    if payload.format in ("apa", "harvard", "mla", "chicago", "sni"):
        citations.sort(key=lambda c: c.lower())

    numbered = "\n".join(f"[{i + 1}] {c}" for i, c in enumerate(citations)) if payload.format in ("ieee", "vancouver") else "\n".join(citations)
    bibliography = numbered if citations else "(belum ada referensi dengan metadata lengkap di grup ini)"

    return BibliographyResponse(
        group_id=group_id,
        format=payload.format,
        citations=citations,
        bibliography=bibliography,
    )


@router.get("/formats")
async def list_formats() -> dict:
    """Daftar format sitasi yang didukung (untuk dropdown UI)."""
    return {"formats": [{"key": key, "label": FORMAT_LABELS[key]} for key in CITATION_FORMATS]}


# ── Pencarian jurnal di internet (Deep Research) ─────────────────────────────


class JournalSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    source: str = Field("all", description="web | arxiv | all")
    max_results: int = Field(5, ge=1, le=20)


class JournalSearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""
    source: str = "web"


@router.post("/search", response_model=list[JournalSearchResult])
async def search_journals(
    payload: JournalSearchRequest,
    current_user: User = Depends(get_current_user),
) -> list[JournalSearchResult]:
    """Cari jurnal di internet (web umum + arXiv) — hasil bisa disimpan ke grup."""
    from app.services.document_tools import arxiv_search, search_web

    results: list[JournalSearchResult] = []
    seen: set[str] = set()
    query = payload.query.strip()

    if payload.source in ("web", "all"):
        try:
            raw = json.loads(await search_web(query, max_results=payload.max_results))
            for item in raw.get("results", []):
                url = (item.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                results.append(
                    JournalSearchResult(
                        title=(item.get("title") or url)[:500],
                        url=url,
                        snippet=(item.get("body") or "")[:400],
                        source="web",
                    )
                )
        except Exception as exc:  # noqa: BLE001 — pencarian tidak boleh mematikan endpoint
            logger.warning("journal search web gagal: %s", exc)

    if payload.source in ("arxiv", "all"):
        try:
            raw = json.loads(await arxiv_search(query, max_results=payload.max_results))
            for item in raw.get("results", []):
                url = (item.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                results.append(
                    JournalSearchResult(
                        title=(item.get("title") or url)[:500],
                        url=url,
                        snippet=(item.get("summary") or "")[:400],
                        source="arxiv",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("journal search arxiv gagal: %s", exc)

    return results[: payload.max_results * 2]


# ── Kategori sitasi ─────────────────────────────────────────


@router.get("/citation-categories", response_model=list[CitationCategoryResponse])
async def list_citation_categories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CitationCategoryResponse]:
    categories = (
        await db.scalars(
            select(CitationCategory)
            .where(CitationCategory.user_id == current_user.id)
            .order_by(CitationCategory.created_at.asc())
        )
    ).all()
    counts = dict(
        (
            await db.execute(
                select(SavedCitation.category_id, func.count())
                .where(SavedCitation.user_id == current_user.id)
                .group_by(SavedCitation.category_id)
            )
        ).all()
    )
    return [_category_response(c, counts.get(c.id, 0)) for c in categories]


@router.post("/citation-categories", response_model=CitationCategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_citation_category(
    payload: CitationCategoryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CitationCategoryResponse:
    """Buat kategori sitasi (mis. 'Sitasi Bab 2')."""
    category = CitationCategory(user_id=current_user.id, name=payload.name.strip())
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return _category_response(category, 0)


@router.put("/citation-categories/{category_id}", response_model=CitationCategoryResponse)
async def update_citation_category(
    category_id: uuid.UUID,
    payload: CitationCategoryUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CitationCategoryResponse:
    category = await _get_owned_category(db, category_id, current_user)
    category.name = payload.name.strip()
    await db.commit()
    await db.refresh(category)
    count = await db.scalar(
        select(func.count()).select_from(SavedCitation).where(
            SavedCitation.category_id == category.id, SavedCitation.user_id == current_user.id
        )
    )
    return _category_response(category, count or 0)


@router.delete("/citation-categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_citation_category(
    category_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hapus kategori beserta sitasi tersimpan di dalamnya (CASCADE)."""
    category = await _get_owned_category(db, category_id, current_user)
    await db.delete(category)
    await db.commit()


# ── Sitasi tersimpan ─────────────────────────────────────────────────────────


@router.get("/citations", response_model=list[SavedCitationResponse])
async def list_saved_citations(
    category_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SavedCitation]:
    stmt = (
        select(SavedCitation)
        .where(SavedCitation.user_id == current_user.id)
        .order_by(SavedCitation.created_at.desc())
    )
    if category_id is not None:
        stmt = stmt.where(SavedCitation.category_id == category_id)
    result = await db.scalars(stmt)
    return list(result.all())


@router.post("/citations", response_model=SavedCitationResponse, status_code=status.HTTP_201_CREATED)
async def save_citation(
    payload: SavedCitationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SavedCitation:
    """Simpan sitasi pilihan user ke kategori."""
    await _get_owned_category(db, payload.category_id, current_user)
    if payload.reference_id is not None:
        await _get_owned_reference(db, payload.reference_id, current_user)
    citation = SavedCitation(
        user_id=current_user.id,
        category_id=payload.category_id,
        reference_id=payload.reference_id,
        format=payload.format,
        citation_text=payload.citation_text.strip(),
        note=(payload.note or "").strip() or None,
    )
    db.add(citation)
    await db.commit()
    await db.refresh(citation)
    return citation


@router.put("/citations/{citation_id}", response_model=SavedCitationResponse)
async def update_saved_citation(
    citation_id: uuid.UUID,
    payload: SavedCitationUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SavedCitation:
    """Pindah kategori / ubah catatan sitasi tersimpan."""
    citation = await _get_owned_saved_citation(db, citation_id, current_user)
    if payload.category_id is not None:
        await _get_owned_category(db, payload.category_id, current_user)
        citation.category_id = payload.category_id
    if payload.note is not None:
        citation.note = payload.note.strip() or None
    await db.commit()
    await db.refresh(citation)
    return citation


@router.delete("/citations/{citation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_citation(
    citation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    citation = await _get_owned_saved_citation(db, citation_id, current_user)
    await db.delete(citation)
    await db.commit()


# ── Simpan referensi dari link (chat) ────────────────────────────────────────


class UrlReferenceRequest(BaseModel):
    url: str = Field(min_length=5)
    group_id: uuid.UUID | None = None
    title_hint: str | None = None


class UrlReferenceResponse(JournalReferenceResponse):
    saved: bool = True


@router.post("/references/from-url", response_model=UrlReferenceResponse, status_code=status.HTTP_201_CREATED)
async def create_reference_from_url(
    background_tasks: BackgroundTasks,
    payload: UrlReferenceRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UrlReferenceResponse:
    """Simpan referensi dari URL/link (mis. hasil chat) — metadata diekstrak AI.

    URL dibuka, teks halaman dikirim ke LLM untuk ekstraksi metadata (sama
    seperti upload PDF), lalu referensi masuk ke grup (default: grup pertama /
    'Referensi Chat').
    """
    from app.services.document_tools import fetch_webpage

    page = None
    try:
        page = json.loads(await fetch_webpage(payload.url, max_chars=6000))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Gagal membuka URL: {exc}")

    page_error = page.get("error") if page else None
    if page_error:
        raise HTTPException(status_code=400, detail=page_error)
    page_text = (page.get("text") or "").strip() if page else ""

    group_id = payload.group_id or await _default_group_id(db, current_user)
    await _get_owned_group(db, group_id, current_user)

    # PRD 3.5: fallback — kalau halaman terlalu pendek, tetap simpan referensi
    # dengan judul dari hint/title URL, status pending (user lengkapi manual).
    page_title = (page or {}).get("title") or ""
    safe_name = f"{payload.title_hint or page_title or 'jurnal'}.url"
    reference = JournalReference(
        user_id=current_user.id,
        group_id=group_id,
        filename=safe_name[:200],
        file_path=payload.url,  # URL disimpan sebagai 'path' sumber
        title=safe_name[:255],
        status="pending",
        error_message=None if len(page_text) >= 200 else "Halaman terlalu pendek — isi metadata manual.",
    )
    db.add(reference)
    await db.commit()
    await db.refresh(reference)

    if len(page_text) < 200:
        # Fallback: simpan saja (tanpa background ekstraksi).
        return UrlReferenceResponse(
            **JournalReferenceResponse.model_validate(reference).model_dump(),
            saved=True,
        )

    try:
        llm = await resolve_llm(db, current_user.id)
        background_tasks.add_task(
            run_extract_metadata,
            reference_id=str(reference.id),
            base_url=llm.base_url,
            api_key=llm.api_key or "dummy",
            model_name=llm.model_name,
            db_url=settings.DATABASE_URL,
        )
    except ModelSelectionError:
        reference.status = "pending"
        reference.error_message = "Model LLM belum dikonfigurasi — isi metadata secara manual."
        await db.commit()
        await db.refresh(reference)

    return UrlReferenceResponse(**JournalReferenceResponse.model_validate(reference).model_dump(), saved=True)
