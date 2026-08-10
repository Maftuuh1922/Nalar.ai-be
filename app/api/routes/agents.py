"""Endpoint CRUD untuk Agents (persona AI kustom)."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.agent import Agent
from app.models.user import User
from app.schemas.agent import AgentCreate, AgentResponse, AgentUpdate

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[AgentResponse])
async def list_agents(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentResponse]:
    """Ambil semua agen milik user yang login."""
    result = await db.scalars(
        select(Agent)
        .where(Agent.user_id == current_user.id)
        .order_by(Agent.created_at.asc())
    )
    db_agents = list(result.all())
    
    from app.services.preset_loader import get_builtin_personas
    presets = get_builtin_personas()
    
    response = []
    # Convert DB agents to AgentResponse format
    for a in db_agents:
        response.append(
            AgentResponse(
                id=a.id,
                user_id=a.user_id,
                name=a.name,
                role=a.role,
                system_prompt=a.system_prompt,
                avatar_icon=a.avatar_icon,
                created_at=a.created_at,
                updated_at=a.updated_at,
                is_builtin=False
            )
        )
    
    # Add presets
    for p in presets:
        response.append(
            AgentResponse(
                id=p.id,
                user_id=current_user.id, # dummy to pass validation
                name=p.name,
                role=p.role,
                system_prompt=p.system_prompt,
                avatar_icon=p.avatar_icon,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                is_builtin=True
            )
        )
    
    return response


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Agent:
    """Buat agen AI baru."""
    # Batasi jumlah agen per user agar tidak overflow
    from sqlalchemy import func
    count = await db.scalar(
        select(func.count(Agent.id)).where(Agent.user_id == current_user.id)
    )
    if count and count >= 20:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Maksimum 20 agen per akun.",
        )

    agent = Agent(
        user_id=current_user.id,
        name=payload.name,
        role=payload.role,
        system_prompt=payload.system_prompt,
        avatar_icon=payload.avatar_icon,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentResponse:
    """Ambil detail satu agen."""
    # Check presets first
    from app.services.preset_loader import get_builtin_personas
    presets = get_builtin_personas()
    for p in presets:
        if p.id == agent_id:
            return AgentResponse(
                id=p.id,
                user_id=current_user.id,
                name=p.name,
                role=p.role,
                system_prompt=p.system_prompt,
                avatar_icon=p.avatar_icon,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                is_builtin=True
            )

    agent = await db.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.user_id == current_user.id)
    )
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agen tidak ditemukan.")
    
    return AgentResponse(
        id=agent.id,
        user_id=agent.user_id,
        name=agent.name,
        role=agent.role,
        system_prompt=agent.system_prompt,
        avatar_icon=agent.avatar_icon,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        is_builtin=False
    )


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: uuid.UUID,
    payload: AgentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Agent:
    """Perbarui agen AI."""
    agent = await db.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.user_id == current_user.id)
    )
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agen tidak ditemukan.")

    if payload.name is not None:
        agent.name = payload.name
    if payload.role is not None:
        agent.role = payload.role
    if payload.system_prompt is not None:
        agent.system_prompt = payload.system_prompt
    if payload.avatar_icon is not None:
        agent.avatar_icon = payload.avatar_icon

    agent.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hapus agen AI."""
    agent = await db.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.user_id == current_user.id)
    )
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agen tidak ditemukan.")
    await db.delete(agent)
    await db.commit()
