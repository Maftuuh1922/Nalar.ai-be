import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import get_current_user, get_db
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseDocument
from app.models.document import Document
from app.models.user import User
from pydantic import BaseModel
from app.schemas.knowledge_base import KnowledgeBaseResponse, KnowledgeBaseCreate, KnowledgeBaseUpdate, KnowledgeBaseDocumentResponse

class AddDocRequest(BaseModel):
    document_id: str

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

async def _get_owned_kb(db, kb_id: uuid.UUID, user: User) -> KnowledgeBase:
    kb = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == kb_id, KnowledgeBase.user_id == user.id))
    if not kb:
        raise HTTPException(status_code=404, detail="Basis pengetahuan tidak ditemukan")
    return kb

@router.get("", response_model=list[KnowledgeBaseResponse])
@router.get("/list", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.scalars(
        select(KnowledgeBase).where(KnowledgeBase.user_id == current_user.id).order_by(KnowledgeBase.created_at.desc())
    )
    return list(result.all())

@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count = await db.scalar(select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.user_id == current_user.id))
    kb = KnowledgeBase(
        user_id=current_user.id,
        name=payload.name,
        engine=payload.engine,
        description=payload.description,
        is_default=(count == 0),
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb

@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _get_owned_kb(db, kb_id, current_user)

@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: uuid.UUID,
    payload: KnowledgeBaseUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_owned_kb(db, kb_id, current_user)
    if payload.name is not None:
        kb.name = payload.name
    if payload.description is not None:
        kb.description = payload.description
    if payload.engine is not None:
        kb.engine = payload.engine
    if payload.is_default is not None:
        if payload.is_default:
            await db.execute(KnowledgeBase.__table__.update().where(KnowledgeBase.user_id == current_user.id).values(is_default=False))
        kb.is_default = payload.is_default
    await db.commit()
    await db.refresh(kb)
    return kb

@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    kb_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_owned_kb(db, kb_id, current_user)
    await db.delete(kb)
    await db.commit()

@router.post("/{kb_id}/documents", status_code=status.HTTP_201_CREATED)
async def add_document_to_kb(
    kb_id: uuid.UUID,
    payload: AddDocRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    document_id = uuid.UUID(payload.document_id)
    kb = await _get_owned_kb(db, kb_id, current_user)
    doc = await db.scalar(select(Document).where(Document.id == document_id, Document.user_id == current_user.id))
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan")
    existing = await db.scalar(
        select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.kb_id == kb_id, KnowledgeBaseDocument.document_id == document_id)
    )
    if existing:
        raise HTTPException(status_code=409, detail="Dokumen sudah ditambahkan ke basis pengetahuan ini")
    kbd = KnowledgeBaseDocument(kb_id=kb_id, document_id=document_id, status="pending")
    db.add(kbd)
    kb.document_count += 1
    await db.commit()
    return {"status": "ok"}

@router.delete("/{kb_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_document_from_kb(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_owned_kb(db, kb_id, current_user)
    kbd = await db.scalar(
        select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.kb_id == kb_id, KnowledgeBaseDocument.document_id == document_id)
    )
    if not kbd:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan di basis pengetahuan ini")
    await db.delete(kbd)
    kb.document_count = max(0, kb.document_count - 1)
    await db.commit()

@router.get("/{kb_id}/documents", response_model=list[KnowledgeBaseDocumentResponse])
async def list_kb_documents(
    kb_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_kb(db, kb_id, current_user)
    result = await db.scalars(
        select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.kb_id == kb_id).order_by(KnowledgeBaseDocument.created_at.desc())
    )
    return list(result.all())

@router.post("/{kb_id}/index", status_code=status.HTTP_200_OK)
async def index_knowledge_base(
    kb_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_owned_kb(db, kb_id, current_user)
    docs = await db.scalars(
        select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.kb_id == kb_id)
    )
    for kbd in docs.all():
        kbd.status = "indexed"
    await db.commit()
    return {"status": "ok", "message": f"Basis pengetahuan '{kb.name}' berhasil diindeks"}
