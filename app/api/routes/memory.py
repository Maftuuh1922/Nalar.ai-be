import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import get_current_user, get_db
from app.models.memory import Memory
from app.models.user import User
from app.schemas.memory import MemoryResponse, MemoryCreate, MemoryStats

router = APIRouter(prefix="/memory", tags=["memory"])

@router.get("", response_model=list[MemoryResponse])
async def list_memories(
    layer: str | None = None,
    surface: str | None = None,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(Memory).where(Memory.user_id == current_user.id)
    if layer:
        query = query.where(Memory.layer == layer)
    if surface:
        query = query.where(Memory.surface == surface)
    query = query.order_by(Memory.created_at.desc()).limit(limit)
    result = await db.scalars(query)
    return list(result.all())

@router.post("", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
async def create_memory(
    payload: MemoryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    memory = Memory(
        user_id=current_user.id,
        layer=payload.layer,
        surface=payload.surface,
        content=payload.content,
        metadata_json=payload.metadata_json,
    )
    db.add(memory)
    await db.commit()
    await db.refresh(memory)
    return memory

@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    memory = await db.scalar(
        select(Memory).where(Memory.id == memory_id, Memory.user_id == current_user.id)
    )
    if not memory:
        raise HTTPException(status_code=404, detail="Memori tidak ditemukan")
    await db.delete(memory)
    await db.commit()

@router.get("/stats", response_model=MemoryStats)
async def get_memory_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    l1 = await db.scalar(select(func.count()).select_from(Memory).where(Memory.user_id == current_user.id, Memory.layer == "L1"))
    l2 = await db.scalar(select(func.count()).select_from(Memory).where(Memory.user_id == current_user.id, Memory.layer == "L2"))
    l3 = await db.scalar(select(func.count()).select_from(Memory).where(Memory.user_id == current_user.id, Memory.layer == "L3"))
    surfaces_q = await db.scalars(select(Memory.surface).where(Memory.user_id == current_user.id).distinct())
    surfaces = list(surfaces_q.all())
    return MemoryStats(l1_count=l1 or 0, l2_count=l2 or 0, l3_count=l3 or 0, surfaces=surfaces)
