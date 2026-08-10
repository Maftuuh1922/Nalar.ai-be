"""Endpoint fitur Riset Mendalam.

Laporan ditulis di latar belakang, jadi endpoint pembuatan langsung membalas
baris berstatus ``pending`` dan frontend memantau progresnya lewat GET.
"""

import os
import re
import tempfile
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.models.notebook import Notebook
from app.models.research_report import ResearchReport
from app.models.user import User
from app.schemas.notebook import NotebookResponse
from app.schemas.research import ResearchCreate, ResearchDetail, ResearchSummary, ResearchToNotebook
from app.services.deep_research import run_research
from app.services.docx_exporter import markdown_to_docx
from app.services.model_selection import ModelSelectionError, resolve_llm

router = APIRouter(prefix="/research", tags=["research"])


async def _get_owned_report(db: AsyncSession, report_id: uuid.UUID, user: User) -> ResearchReport:
    report = await db.scalar(
        select(ResearchReport).where(
            ResearchReport.id == report_id, ResearchReport.user_id == user.id
        )
    )
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Riset tidak ditemukan.")
    return report


@router.get("", response_model=list[ResearchSummary])
async def list_reports(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ResearchReport]:
    """Daftar riset milik user, terbaru di atas."""
    result = await db.scalars(
        select(ResearchReport)
        .where(ResearchReport.user_id == current_user.id)
        .order_by(ResearchReport.created_at.desc())
    )
    return list(result.all())


@router.post("", response_model=ResearchDetail, status_code=status.HTTP_201_CREATED)
async def create_report(
    payload: ResearchCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchReport:
    """Mulai riset baru; penulisan laporan berjalan di latar belakang."""
    try:
        llm = await resolve_llm(db, current_user.id)
    except ModelSelectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    report = ResearchReport(
        user_id=current_user.id,
        topic=payload.topic.strip(),
        instructions=(payload.instructions or "").strip() or None,
        depth=payload.depth,
        status="pending",
        progress_step="Menunggu antrean",
        progress_percent=0,
        content_markdown="",
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)

    background_tasks.add_task(
        run_research,
        report_id=str(report.id),
        base_url=llm.base_url,
        # Endpoint lokal boleh tanpa API key, sama seperti di menu chat.
        api_key=llm.api_key or "dummy",
        model_name=llm.model_name,
        db_url=settings.DATABASE_URL,
    )
    return report


@router.get("/{report_id}", response_model=ResearchDetail)
async def get_report(
    report_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchReport:
    """Detail satu riset, termasuk isi laporan bila sudah selesai."""
    return await _get_owned_report(db, report_id, current_user)


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report(
    report_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hapus riset beserta laporannya."""
    report = await _get_owned_report(db, report_id, current_user)
    await db.delete(report)
    await db.commit()


@router.get("/{report_id}/export/docx")
async def export_report_docx(
    report_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unduh laporan sebagai berkas Word (.docx)."""
    report = await _get_owned_report(db, report_id, current_user)
    if report.status != "completed" or not report.content_markdown.strip():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Laporan belum selesai ditulis.",
        )

    safe_title = re.sub(r'[\\/:*?"<>|]', "_", report.topic).strip()[:80] or "Laporan_Riset"
    output_path = os.path.join(
        tempfile.gettempdir(), f"{safe_title.replace(' ', '_')}_{uuid.uuid4().hex[:6]}.docx"
    )
    try:
        markdown_to_docx(report.content_markdown, output_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gagal membuat berkas Word: {exc}")

    return FileResponse(
        path=output_path,
        filename=f"{safe_title}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.post("/{report_id}/to-notebook", response_model=NotebookResponse, status_code=status.HTTP_201_CREATED)
async def send_report_to_notebook(
    report_id: uuid.UUID,
    payload: ResearchToNotebook,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Notebook:
    """Salin laporan ke menu Catatan supaya bisa disunting lebih lanjut."""
    report = await _get_owned_report(db, report_id, current_user)
    if report.status != "completed" or not report.content_markdown.strip():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Laporan belum selesai ditulis.",
        )

    notebook = Notebook(
        user_id=current_user.id,
        title=(payload.title or report.topic)[:255],
        content=report.content_markdown,
    )
    db.add(notebook)
    await db.commit()
    await db.refresh(notebook)
    return notebook
