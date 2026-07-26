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
from app.services.preferences import build_http_client, get_preferences
from pydantic import BaseModel

class SuggestionRequest(BaseModel):
    agent_id: str | None = None

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

    # Setelan dari Pengaturan > Percakapan & Jaringan
    prefs = await get_preferences(db, current_user.id)

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

    # Load history for the session (limit to last 20 messages to save context)
    chat_history_list = []
    if session_id:
        from app.models.chat_history import ChatHistory
        history_records = await db.scalars(
            select(ChatHistory)
            .where(ChatHistory.session_id == session_id)
            .order_by(ChatHistory.created_at.asc())
        )
        history_window = max(0, prefs.history_limit)
        all_records = history_records.all()
        for record in (all_records[-history_window:] if history_window else []):
            if getattr(record, 'images_json', None):
                try:
                    images = json.loads(record.images_json)
                    content_parts = [{"type": "text", "text": record.content}]
                    for img in images:
                        content_parts.append({"type": "image_url", "image_url": {"url": img}})
                    chat_history_list.append({"role": record.role, "content": content_parts})
                except Exception:
                    chat_history_list.append({"role": record.role, "content": record.content})
            else:
                chat_history_list.append({"role": record.role, "content": record.content})

    # Simpan pesan user ke history
    user_msg = ChatHistory(
        user_id=current_user.id,
        session_id=session_id,
        role="user",
        content=payload.message,
        images_json=json.dumps(payload.images) if payload.images else None
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

    # Inisialisasi client OpenAI. http_client hanya dibuat kalau user memakai
    # proxy; kalau None, SDK memakai client bawaannya.
    proxy_client = build_http_client(prefs)
    client = AsyncOpenAI(
        api_key=api_key or "dummy",
        base_url=model_cfg.base_url,
        timeout=float(prefs.request_timeout),
        http_client=proxy_client,
    )

    # Beri tahu AI dokumen apa saja yang spesifik dipilih user
    if document_ids:
        try:
            docs_for_context = await db.scalars(
                select(Document).where(Document.id.in_([uuid.UUID(d) for d in document_ids]))
            )
            doc_list_str = "\n".join([f"- ID: {d.id} | File: {d.filename}" for d in docs_for_context.all()])
            doc_system_instruction = (
                f"PENTING: User telah menunjuk dokumen berikut untuk sesi ini:\n{doc_list_str}\n"
                f"Kamu WAJIB menggunakan tool 'read_document' atau 'search_in_document' pada dokumen di atas "
                f"jika user meminta penjelasan atau bertanya tentang konteks yang ada di dalamnya!"
            )
            if agent_system_prompt:
                agent_system_prompt += "\n\n" + doc_system_instruction
            else:
                agent_system_prompt = doc_system_instruction
        except Exception as e:
            import logging
            logging.error(f"Error fetching docs for context: {e}")

    async def stream_generator() -> AsyncGenerator[str, None]:
        full_text = ""
        final_usage = None
        try:
            async for chunk in run_agentic_chat_stream(
                client=client,
                model_name=model_cfg.model_name,
                user_message=payload.message,
                user_images=payload.images,
                agent_system_prompt=agent_system_prompt,
                db=db,
                user_id=current_user.id,
                chat_history=chat_history_list,
                enable_rtk=payload.enable_rtk,
                max_tokens=prefs.chat_max_tokens,
                temperature=prefs.chat_temperature,
                enable_web_tools=prefs.enable_web_tools,
                enable_document_tools=prefs.enable_document_tools,
                custom_instructions=prefs.custom_instructions,
                retrieval_top_k=prefs.retrieval_top_k,
            ):
                # Try to parse the chunk to accumulate text
                try:
                    event_obj = json.loads(chunk.strip())
                    if event_obj.get("event") == "text":
                        full_text += event_obj.get("data", "")
                    elif event_obj.get("event") == "usage":
                        final_usage = event_obj.get("data")
                except:
                    pass
                yield chunk
                
            # Generate suggestions inline (bisa dimatikan lewat Pengaturan)
            if full_text and prefs.enable_suggestions:
                try:
                    sugg_prompt = f"Berdasarkan jawaban ini:\n\n{full_text[:1500]}\n\nBerikan HANYA 3 saran pertanyaan singkat lanjutan dalam format JSON array string. Contoh: [\"Apa itu X?\", \"Bagaimana cara Y?\", \"Jelaskan Z\"]"
                    sugg_response = await client.chat.completions.create(
                        model=model_cfg.model_name,
                        messages=[{"role": "user", "content": sugg_prompt}],
                        temperature=0.7,
                        max_tokens=150
                    )
                    sugg_content = sugg_response.choices[0].message.content.strip()
                    if sugg_content.startswith("```json"):
                        sugg_content = sugg_content.replace("```json", "").replace("```", "").strip()
                    sugg_list = json.loads(sugg_content)
                    if isinstance(sugg_list, list):
                        yield json.dumps({"event": "suggestions", "data": sugg_list[:3]}) + "\n"
                except Exception as e:
                    import logging
                    logging.error(f"Failed to generate inline suggestions: {e}")
                    pass
                    
        except Exception as exc:
            yield json.dumps({"event": "error", "data": f"Gagal menghubungi model AI: {exc}"}) + "\n"
        finally:
            # Simpan jawaban AI ke history
            ai_msg = ChatHistory(
                user_id=current_user.id,
                session_id=session_id,
                role="assistant",
                content=full_text,
                usage_json=json.dumps(final_usage) if final_usage else None
            )
            db.add(ai_msg)
            await db.commit()
            if proxy_client is not None:
                await proxy_client.aclose()

    return StreamingResponse(stream_generator(), media_type="application/x-ndjson")

@router.post("/{session_id}/suggestions", response_model=list[str], status_code=status.HTTP_200_OK)
async def get_chat_suggestions(
    session_id: uuid.UUID,
    payload: SuggestionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mendapatkan 3 saran pertanyaan berdasarkan percakapan terakhir."""
    model_cfg = await db.scalar(
        select(ModelConfig).where(
            ModelConfig.user_id == current_user.id,
            ModelConfig.is_active == True
        )
    )
    if not model_cfg:
        return []

    prefs = await get_preferences(db, current_user.id)
    if not prefs.enable_suggestions:
        return []

    # Get last assistant message
    history_records = await db.scalars(
        select(ChatHistory)
        .where(ChatHistory.session_id == session_id)
        .order_by(ChatHistory.created_at.desc())
        .limit(2)
    )
    history = list(history_records.all())
    if not history or history[0].role != "assistant":
        return []

    last_assistant_msg = history[0].content

    api_key = decrypt_api_key(model_cfg.api_key_encrypted)
    proxy_client = build_http_client(prefs)
    client = AsyncOpenAI(
        api_key=api_key or "dummy",
        base_url=model_cfg.base_url,
        timeout=float(prefs.request_timeout),
        http_client=proxy_client,
    )

    prompt = f"Berdasarkan jawaban terakhir asisten AI ini:\n\n{last_assistant_msg[:2000]}\n\nBuatlah tepat 3 saran pertanyaan lanjutan singkat (maksimal 10 kata per pertanyaan) yang relevan untuk ditanyakan oleh pengguna. Format output HANYA array JSON string tanpa markdown, contoh: [\"Apa maksud dari X?\", \"Bagaimana cara Y?\", \"Jelaskan Z\"]"

    try:
        response = await client.chat.completions.create(
            model=model_cfg.model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=150
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```json"):
            content = content.replace("```json", "").replace("```", "").strip()
        suggestions = json.loads(content)
        if isinstance(suggestions, list):
            return suggestions[:3]
        return []
    except Exception as e:
        import logging
        logging.error(f"Error generating suggestions: {e}")
        return []
    finally:
        if proxy_client is not None:
            await proxy_client.aclose()





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


class ChatSessionUpdate(BaseModel):
    title: str


@router.put("/sessions/{session_id}", response_model=ChatSessionResponse)
async def rename_chat_session(
    session_id: uuid.UUID,
    payload: ChatSessionUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatSession:
    """Ubah judul sebuah sesi chat."""
    sess = await db.scalar(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == current_user.id)
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan")
    sess.title = payload.title.strip()[:255] or sess.title
    await db.commit()
    await db.refresh(sess)
    return sess


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
