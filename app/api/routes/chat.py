"""Endpoint chat RAG."""

import json
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from openai import AsyncOpenAI

from app.api.deps import get_current_user
from app.core.encryption import decrypt_api_key
from app.db.session import get_db
from app.models.chat_history import ChatHistory
from app.models.document import Document
from app.models.model_config import ModelConfig
from app.models.user import User
from app.models.chat_session import ChatSession
from app.schemas.chat import ChatHistoryItem, ChatRequest, ChatResponse, Source
from app.schemas.chat_session import ChatSessionResponse, ChatSessionCreate
from app.services.agentic_chat import run_agentic_chat_stream

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse, status_code=status.HTTP_200_OK)
async def chat(
    payload: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """Tanya-jawab berbasis dokumen menggunakan RAG."""
    # Cek konfigurasi model AI
    model_cfg = await db.scalar(
        select(ModelConfig).where(
            ModelConfig.user_id == current_user.id,
            ModelConfig.is_active == True
        )
    )
    if model_cfg is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tidak ada konfigurasi model AI yang aktif. Silakan atur dan aktifkan konfigurasi di halaman Pengaturan.",
        )

    from sqlalchemy import func as sql_func
    # Cek apakah ada dokumen yang sudah terindeks
    indexed_count = await db.scalar(
        select(sql_func.count()).select_from(Document).where(
            Document.user_id == current_user.id,
            Document.status == "indexed",
        )
    )

    # Handle session
    session_id = payload.session_id
    if not session_id:
        title = payload.message[:50] + ("..." if len(payload.message) > 50 else "")
        new_session = ChatSession(
            user_id=current_user.id,
            title=title,
        )
        db.add(new_session)
        await db.flush()
        session_id = new_session.id
    else:
        from sqlalchemy import func as sql_func
        sess = await db.scalar(select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == current_user.id))
        if not sess:
            raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan")

    # Simpan pesan user ke history
    user_msg = ChatHistory(
        user_id=current_user.id,
        session_id=session_id,
        role="user",
        content=payload.message,
    )
    db.add(user_msg)
    await db.flush()

    # Ambil system_prompt dari agent jika disediakan
    agent_system_prompt: str | None = None
    if payload.agent_id:
        from app.models.agent import Agent
        agent = await db.scalar(
            select(Agent).where(Agent.id == payload.agent_id, Agent.user_id == current_user.id)
        )
        if agent:
            agent_system_prompt = agent.system_prompt

    # Jalankan RAG query
    api_key = decrypt_api_key(model_cfg.api_key_encrypted)
    document_ids = [str(d) for d in payload.document_ids] if payload.document_ids is not None else None

    # Ambil semua dokumen user jika document_ids tidak dispesifikasikan secara khusus
    if document_ids is None:
        user_docs = await db.scalars(
            select(Document).where(Document.user_id == current_user.id)
        )
        all_user_doc_ids = [str(d.id) for d in user_docs.all()]
        if all_user_doc_ids:
            document_ids = all_user_doc_ids

    # Inisialisasi client OpenAI
    client = AsyncOpenAI(
        api_key=api_key or "dummy",
        base_url=model_cfg.base_url,
    )

    async def stream_generator() -> AsyncGenerator[str, None]:
        full_text = ""
        try:
            async for chunk in run_agentic_chat_stream(
                client=client,
                model_name=model_cfg.model_name,
                user_message=payload.message,
                agent_system_prompt=agent_system_prompt,
                db=db,
                user_id=current_user.id
            ):
                # Try to parse the chunk to accumulate text
                try:
                    event_obj = json.loads(chunk.strip())
                    if event_obj.get("event") == "text":
                        full_text += event_obj.get("data", "")
                except:
                    pass
                yield chunk
        except Exception as exc:
            yield json.dumps({"event": "error", "data": f"Gagal menghubungi model AI: {exc}"}) + "\n"
        finally:
            # Simpan jawaban AI ke history
            ai_msg = ChatHistory(
                user_id=current_user.id,
                session_id=session_id,
                role="assistant",
                content=full_text,
            )
            db.add(ai_msg)
            await db.commit()

    return StreamingResponse(stream_generator(), media_type="application/x-ndjson")




@router.get("/sessions", response_model=list[ChatSessionResponse])
async def get_chat_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChatSession]:
    """Ambil semua riwayat sesi chat user."""
    result = await db.scalars(
        select(ChatSession)
        .where(ChatSession.user_id == current_user.id)
        .order_by(ChatSession.updated_at.desc())
    )
    return list(result.all())


@router.delete("/sessions/{session_id}")
async def delete_chat_session(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Hapus sebuah sesi chat."""
    sess = await db.scalar(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == current_user.id)
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan")
    await db.delete(sess)
    await db.commit()
    return {"status": "ok"}


@router.get("/sessions/{session_id}/history", response_model=list[ChatHistoryItem])
async def get_session_chat_history(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChatHistory]:
    """Ambil riwayat chat user berdasarkan sesi (50 pesan terakhir)."""
    # Pastikan sesi milik user
    sess = await db.scalar(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == current_user.id)
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan")

    result = await db.scalars(
        select(ChatHistory)
        .where(ChatHistory.user_id == current_user.id, ChatHistory.session_id == session_id)
        .order_by(ChatHistory.created_at.asc())
        .limit(100)
    )
    return list(result.all())
