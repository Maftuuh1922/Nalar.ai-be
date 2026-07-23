"""Endpoint CRUD untuk Notebooks."""

import uuid
import os
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.notebook import Notebook
from app.models.user import User
from app.schemas.notebook import NotebookCreate, NotebookResponse, NotebookUpdate, DocxExportRequest
from app.services.docx_exporter import markdown_to_docx

router = APIRouter(prefix="/notebooks", tags=["notebooks"])


@router.get("", response_model=list[NotebookResponse])
async def list_notebooks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Notebook]:
    """Ambil semua catatan milik user yang login."""
    result = await db.scalars(
        select(Notebook)
        .where(Notebook.user_id == current_user.id)
        .order_by(Notebook.updated_at.desc())
    )
    return list(result.all())


@router.post("", response_model=NotebookResponse, status_code=status.HTTP_201_CREATED)
async def create_notebook(
    payload: NotebookCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Notebook:
    """Buat catatan baru."""
    notebook = Notebook(
        user_id=current_user.id,
        title=payload.title,
        content=payload.content,
    )
    db.add(notebook)
    await db.commit()
    await db.refresh(notebook)
    return notebook


@router.get("/{notebook_id}", response_model=NotebookResponse)
async def get_notebook(
    notebook_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Notebook:
    """Ambil detail satu catatan."""
    notebook = await db.scalar(
        select(Notebook).where(Notebook.id == notebook_id, Notebook.user_id == current_user.id)
    )
    if not notebook:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catatan tidak ditemukan.")
    return notebook


@router.put("/{notebook_id}", response_model=NotebookResponse)
async def update_notebook(
    notebook_id: uuid.UUID,
    payload: NotebookUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Notebook:
    """Perbarui catatan (auto-save akan memanggil endpoint ini)."""
    notebook = await db.scalar(
        select(Notebook).where(Notebook.id == notebook_id, Notebook.user_id == current_user.id)
    )
    if not notebook:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catatan tidak ditemukan.")

    if payload.title is not None:
        notebook.title = payload.title
    if payload.content is not None:
        notebook.content = payload.content

    notebook.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(notebook)
    return notebook


@router.delete("/{notebook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notebook(
    notebook_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hapus catatan."""
    notebook = await db.scalar(
        select(Notebook).where(Notebook.id == notebook_id, Notebook.user_id == current_user.id)
    )
    if not notebook:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catatan tidak ditemukan.")
    
    await db.delete(notebook)
    await db.commit()


@router.post("/export/docx")
async def export_to_docx(
    request: DocxExportRequest,
    current_user: User = Depends(get_current_user)
):
    """Export markdown text to a Word Document (.docx)"""
    temp_dir = tempfile.gettempdir()
    output_filename = f"{request.title.replace(' ', '_')}_{uuid.uuid4().hex[:6]}.docx"
    output_path = os.path.join(temp_dir, output_filename)
    
    try:
        # Convert markdown to docx
        markdown_to_docx(request.content, output_path)
        
        return FileResponse(
            path=output_path,
            filename=f"{request.title}.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate docx: {str(e)}")
