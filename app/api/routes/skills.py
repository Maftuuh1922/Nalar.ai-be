import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import get_current_user, get_db
from app.models.skill import Skill
from app.models.user import User
from app.schemas.skill import SkillResponse, SkillCreate, SkillUpdate

router = APIRouter(prefix="/skills", tags=["skills"])

@router.get("", response_model=list[SkillResponse])
async def list_skills(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.scalars(
        select(Skill).where(Skill.user_id == current_user.id).order_by(Skill.created_at.desc())
    )
    return list(result.all())

@router.post("", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
async def create_skill(
    payload: SkillCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    skill = Skill(
        user_id=current_user.id,
        name=payload.name,
        description=payload.description,
        content=payload.content,
        source=payload.source,
        tags=payload.tags,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return skill

@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill(
    skill_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    skill = await db.scalar(select(Skill).where(Skill.id == skill_id, Skill.user_id == current_user.id))
    if not skill:
        raise HTTPException(status_code=404, detail="Skill tidak ditemukan")
    return skill

@router.put("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: uuid.UUID,
    payload: SkillUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    skill = await db.scalar(select(Skill).where(Skill.id == skill_id, Skill.user_id == current_user.id))
    if not skill:
        raise HTTPException(status_code=404, detail="Skill tidak ditemukan")
    if payload.name is not None:
        skill.name = payload.name
    if payload.description is not None:
        skill.description = payload.description
    if payload.content is not None:
        skill.content = payload.content
    if payload.is_active is not None:
        skill.is_active = payload.is_active
    if payload.tags is not None:
        skill.tags = payload.tags
    await db.commit()
    await db.refresh(skill)
    return skill

@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    skill = await db.scalar(select(Skill).where(Skill.id == skill_id, Skill.user_id == current_user.id))
    if not skill:
        raise HTTPException(status_code=404, detail="Skill tidak ditemukan")
    await db.delete(skill)
    await db.commit()
