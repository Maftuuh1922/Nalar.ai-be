import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import get_current_user, get_db
from app.models.question_bank import Question
from app.models.user import User
from app.schemas.question import QuestionResponse, QuestionCreate, QuestionUpdate

router = APIRouter(prefix="/questions", tags=["questions"])

@router.get("", response_model=list[QuestionResponse])
async def list_questions(
    topic: str | None = None,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(Question).where(Question.user_id == current_user.id)
    if topic:
        query = query.where(Question.topic.ilike(f"%{topic}%"))
    query = query.order_by(Question.created_at.desc()).limit(limit)
    result = await db.scalars(query)
    return list(result.all())

@router.post("", response_model=QuestionResponse, status_code=status.HTTP_201_CREATED)
async def create_question(
    payload: QuestionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = Question(
        user_id=current_user.id,
        topic=payload.topic,
        question=payload.question,
        user_answer=payload.user_answer,
        reference_answer=payload.reference_answer,
        explanation=payload.explanation,
        tags=payload.tags,
    )
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q

@router.get("/{question_id}", response_model=QuestionResponse)
async def get_question(
    question_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = await db.scalar(select(Question).where(Question.id == question_id, Question.user_id == current_user.id))
    if not q:
        raise HTTPException(status_code=404, detail="Pertanyaan tidak ditemukan")
    return q

@router.put("/{question_id}", response_model=QuestionResponse)
async def update_question(
    question_id: uuid.UUID,
    payload: QuestionUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = await db.scalar(select(Question).where(Question.id == question_id, Question.user_id == current_user.id))
    if not q:
        raise HTTPException(status_code=404, detail="Pertanyaan tidak ditemukan")
    if payload.user_answer is not None:
        q.user_answer = payload.user_answer
    if payload.reference_answer is not None:
        q.reference_answer = payload.reference_answer
    if payload.explanation is not None:
        q.explanation = payload.explanation
    if payload.tags is not None:
        q.tags = payload.tags
    await db.commit()
    await db.refresh(q)
    return q

@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_question(
    question_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = await db.scalar(select(Question).where(Question.id == question_id, Question.user_id == current_user.id))
    if not q:
        raise HTTPException(status_code=404, detail="Pertanyaan tidak ditemukan")
    await db.delete(q)
    await db.commit()

@router.get("/topics/list", response_model=list[str])
async def list_question_topics(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.scalars(select(Question.topic).where(Question.user_id == current_user.id).distinct().order_by(Question.topic))
    return list(result.all())
