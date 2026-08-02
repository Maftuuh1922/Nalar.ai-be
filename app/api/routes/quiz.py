from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.document import Document
from app.models.quiz import Quiz
from app.models.quiz_attempt import QuizAttempt
from app.models.user import User
from app.schemas.quiz import QuizCreate, QuizResponse
from app.schemas.quiz_attempt import QuizAttemptCreate, QuizAttemptResponse
from app.services.model_selection import ModelSelectionError, resolve_embedding, resolve_llm
from app.services.preferences import get_preferences
from app.services.rag import generate_quiz

router = APIRouter(prefix="/quizzes", tags=["quizzes"])


@router.post("/generate", response_model=QuizResponse)
async def create_quiz(
    *,
    db: AsyncSession = Depends(get_db),
    quiz_in: QuizCreate,
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Generate latihan soal menggunakan RAG berdasarkan dokumen.
    """
    # Dokumen bersifat opsional — tanpa dokumen, soal dibuat dari topik bebas.
    doc = None
    if quiz_in.document_id is not None:
        doc = await db.scalar(
            select(Document).where(Document.id == quiz_in.document_id, Document.user_id == current_user.id)
        )
        if not doc:
            raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan")
        if doc.status != "indexed":
            raise HTTPException(status_code=400, detail="Dokumen belum selesai diindeks")

    # Ambil konfigurasi model AI milik user dari katalog
    try:
        llm = await resolve_llm(db, current_user.id)
        emb = await resolve_embedding(db, current_user.id)
    except ModelSelectionError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    # Luas pengambilan materi mengikuti Pengaturan > Pusat Pengetahuan.
    prefs = await get_preferences(db, current_user.id)

    try:
        # Generate soal dari LLM
        questions_data = await generate_quiz(
            user_id=str(current_user.id),
            document_id=str(doc.id) if doc else None,
            topic=quiz_in.topic,
            num_questions=quiz_in.num_questions,
            base_url=llm.base_url,
            api_key=llm.api_key or "dummy",
            model_name=llm.model_name,
            embedding_model=emb.model_name,
            top_k=prefs.retrieval_top_k,
            embedding_base_url=emb.base_url,
            embedding_api_key=emb.api_key or "dummy",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Terjadi kesalahan saat generate soal: {e}")

    if not questions_data:
        raise HTTPException(status_code=400, detail="Model AI tidak menghasilkan satu soal pun. Silakan coba lagi.")

    # Simpan kuis ke DB
    quiz = Quiz(
        user_id=current_user.id,
        document_id=doc.id if doc else None,
        topic=quiz_in.topic,
        questions_data=questions_data,
    )
    db.add(quiz)
    await db.commit()
    await db.refresh(quiz)

    return quiz


@router.get("", response_model=list[QuizResponse])
async def read_quizzes(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Ambil semua kuis milik user.
    """
    result = await db.execute(
        select(Quiz)
        .where(Quiz.user_id == current_user.id)
        .order_by(Quiz.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()


@router.get("/{id}", response_model=QuizResponse)
async def read_quiz(
    id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Ambil detail kuis.
    """
    quiz = await db.scalar(
        select(Quiz).where(Quiz.id == id, Quiz.user_id == current_user.id)
    )
    if not quiz:
        raise HTTPException(status_code=404, detail="Kuis tidak ditemukan")
    return quiz


@router.delete("/{id}")
async def delete_quiz(
    id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Hapus kuis.
    """
    quiz = await db.scalar(
        select(Quiz).where(Quiz.id == id, Quiz.user_id == current_user.id)
    )
    if not quiz:
        raise HTTPException(status_code=404, detail="Kuis tidak ditemukan")
    
    await db.delete(quiz)
    await db.commit()
    return {"status": "ok"}


@router.post("/{id}/attempts", response_model=QuizAttemptResponse)
async def record_quiz_attempt(
    id: UUID,
    attempt_in: QuizAttemptCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Simpan riwayat skor kuis."""
    quiz = await db.scalar(
        select(Quiz).where(Quiz.id == id, Quiz.user_id == current_user.id)
    )
    if not quiz:
        raise HTTPException(status_code=404, detail="Kuis tidak ditemukan")

    attempt = QuizAttempt(
        quiz_id=id,
        user_id=current_user.id,
        score_percentage=attempt_in.score_percentage,
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return attempt
