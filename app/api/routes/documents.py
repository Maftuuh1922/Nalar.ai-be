"""Endpoint upload dan manajemen dokumen."""

import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, status, File
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.models.document import Document
from app.models.user import User
from app.schemas.document import DocumentResponse
from app.services import rag
from app.services.model_selection import ModelSelectionError, resolve_embedding
from app.services.preferences import get_preferences

router = APIRouter(prefix="/documents", tags=["documents"])

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".docx"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


async def _run_indexing(
    doc_id: str,
    user_id: str,
    file_path: str,
    base_url: str,
    api_key: str,
    embedding_model: str,
    db_url: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> None:
    """Background task: index dokumen dan perbarui status di DB."""
    # Buat session DB baru untuk background task
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db.base import Base

    engine = create_async_engine(db_url, echo=False)
    SessionLocal = async_sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    async with SessionLocal() as db:
        doc = await db.get(Document, uuid.UUID(doc_id))
        if doc is None:
            return
        try:
            await rag.index_document(
                user_id=user_id,
                file_path=file_path,
                doc_id=doc_id,
                base_url=base_url,
                api_key=api_key,
                embedding_model=embedding_model,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            # Cek ulang sebelum commit — dokumen mungkin sudah dihapus user
            # selagi indexing berjalan (delete di endpoint lain).
            still_exists = await db.get(Document, uuid.UUID(doc_id))
            if still_exists is None:
                return
            doc.status = "indexed"
        except Exception as exc:
            doc.status = "failed"
            doc.error_message = str(exc)[:900]
        try:
            await db.commit()
        except Exception:  # noqa: BLE001 — row sudah dihapus; abaikan
            await db.rollback()

    await engine.dispose()


@router.get("", response_model=list[DocumentResponse])
async def list_documents(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Document]:
    """Daftar semua dokumen milik user."""
    result = await db.scalars(
        select(Document).where(Document.user_id == current_user.id).order_by(Document.created_at.desc())
    )
    return list(result.all())


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Document:
    """Upload dokumen PDF/TXT dan mulai proses indexing di background."""
    # Validasi ekstensi
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Format tidak didukung. Gunakan: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    # Cek konfigurasi model embedding AI milik user
    try:
        emb = await resolve_embedding(db, current_user.id)
    except ModelSelectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    # Baca dan validasi ukuran file
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Ukuran file maksimal 50 MB.",
        )

    # Simpan file ke disk
    doc_id = uuid.uuid4()
    user_dir = Path(settings.UPLOAD_DIR) / str(current_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{doc_id}{suffix}"
    file_path = user_dir / safe_name
    file_path.write_bytes(content)

    # Setelan potongan dokumen dari Pengaturan > Pusat Pengetahuan
    prefs = await get_preferences(db, current_user.id)

    # Simpan record ke DB
    doc = Document(
        id=doc_id,
        user_id=current_user.id,
        filename=file.filename or safe_name,
        file_path=str(file_path),
        status="pending",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    # Jalankan indexing di background
    background_tasks.add_task(
        _run_indexing,
        doc_id=str(doc_id),
        user_id=str(current_user.id),
        file_path=str(file_path),
        base_url=emb.base_url,
        api_key=emb.api_key or "dummy",
        embedding_model=emb.model_name,
        db_url=settings.DATABASE_URL,
        chunk_size=prefs.chunk_size,
        chunk_overlap=prefs.chunk_overlap,
    )

    return doc


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hapus dokumen beserta vektornya dari ChromaDB."""
    doc = await db.scalar(
        select(Document).where(Document.id == doc_id, Document.user_id == current_user.id)
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dokumen tidak ditemukan.")

    # Hapus vektor dari ChromaDB (sync, tapi cepat)
    rag.delete_document_vectors(user_id=str(current_user.id), doc_id=str(doc_id))

    # Hapus file dari disk
    try:
        Path(doc.file_path).unlink(missing_ok=True)
    except Exception:
        pass

    await db.delete(doc)
    await db.commit()

@router.get("/view")
async def view_document(
    filename: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Serve the document file for viewing in the browser."""
    from fastapi.responses import FileResponse
    # Cari dokumen berdasarkan filename dan user_id
    doc = await db.scalar(
        select(Document).where(Document.filename == filename, Document.user_id == current_user.id).order_by(Document.created_at.desc())
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dokumen tidak ditemukan.")
    
    # Resolve path - bisa relatif terhadap root project (di mana server dijalankan)
    BASE_DIR = Path(__file__).resolve().parents[3]  # naik ke root project (Nalar.ai-be/)
    file_path = Path(doc.file_path)
    if not file_path.is_absolute():
        file_path = BASE_DIR / file_path
    
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File fisik tidak ditemukan: {file_path}")
        
    return FileResponse(path=str(file_path), filename=doc.filename, content_disposition_type="inline")
