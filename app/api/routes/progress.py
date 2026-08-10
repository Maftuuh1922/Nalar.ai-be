from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.chat_history import ChatHistory
from app.models.quiz import Quiz
from app.models.quiz_attempt import QuizAttempt
from app.models.user import User
from app.models.document import Document
from app.services.mastery import compute_mastery, mastery_level_label

router = APIRouter(prefix="/progress", tags=["progress"])


@router.get("/stats")
async def get_progress_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Ambil statistik progress belajar user beserta tingkat penguasaan (Mastery Level)."""
    
    # 1. Skor rata-rata kuis secara keseluruhan
    avg_score = await db.scalar(
        select(func.avg(QuizAttempt.score_percentage))
        .where(QuizAttempt.user_id == current_user.id)
    )
    
    # 2. Ambil semua riwayat kuis untuk menghitung Nalar AI Recency-Weighted Mastery Score
    attempts_query = await db.execute(
        select(QuizAttempt, Quiz.topic)
        .join(Quiz, QuizAttempt.quiz_id == Quiz.id)
        .where(QuizAttempt.user_id == current_user.id)
        .order_by(QuizAttempt.created_at.asc())
    )
    all_attempts = attempts_query.all()

    # Kelompokkan attempt per topik untuk menghitung penguasaan spesifik per topik
    topic_attempts: dict[str, list[bool]] = {}
    overall_correctness: list[bool] = []
    recent_attempts = []

    for attempt, topic in all_attempts:
        is_pass = attempt.score_percentage >= 70
        overall_correctness.append(is_pass)
        if topic not in topic_attempts:
            topic_attempts[topic] = []
        topic_attempts[topic].append(is_pass)

    # Hitung mastery score keseluruhan menggunakan algoritma Nalar AI
    overall_mastery = compute_mastery(overall_correctness)
    overall_level = mastery_level_label(overall_mastery)

    # Breakdown per topik
    topic_mastery_list = []
    for topic, correctness in topic_attempts.items():
        score = compute_mastery(correctness)
        topic_mastery_list.append({
            "topic": topic,
            "mastery_score": round(score * 100, 1),
            "level": mastery_level_label(score),
            "total_attempts": len(correctness),
        })

    # Ambil 20 percobaan kuis terakhir untuk grafik
    for attempt, topic in all_attempts[-20:]:
        recent_attempts.append({
            "id": str(attempt.id),
            "quiz_id": str(attempt.quiz_id),
            "topic": topic,
            "score_percentage": attempt.score_percentage,
            "created_at": attempt.created_at,
        })
        
    total_docs = await db.scalar(
        select(func.count(Document.id))
        .where(Document.user_id == current_user.id)
    )
    
    from app.models.chat_session import ChatSession
    total_sessions = await db.scalar(
        select(func.count(ChatSession.id))
        .where(ChatSession.user_id == current_user.id)
    )
    
    total_quizzes = await db.scalar(
        select(func.count(Quiz.id))
        .where(Quiz.user_id == current_user.id)
    )

    return {
        "average_score": float(avg_score) if avg_score else 0.0,
        "mastery_score_percentage": round(overall_mastery * 100, 1),
        "mastery_level": overall_level,
        "topic_mastery": topic_mastery_list,
        "recent_attempts": recent_attempts,
        "total_documents": total_docs or 0,
        "total_sessions": total_sessions or 0,
        "total_quizzes": total_quizzes or 0,
    }

