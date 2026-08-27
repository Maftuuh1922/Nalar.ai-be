"""Stub endpoints untuk meng-cover FE calls yang missing di BE.

FE memanggil banyak endpoint yang belum ada di BE. File ini menambahkan
stub/placeholder endpoints agar FE tidak 404/405. Endpoint yang memerlukan
logika nyata nanti diisi.
"""
import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_optional_user, get_db
from app.models.user import User
from app.models.notebook import Notebook
from app.models.memory import Memory
from app.models.journal import JournalGroup, JournalReference

logger = logging.getLogger(__name__)

# ── Memory workbench stubs ──────────────────────────────────────────────
memory_extra = APIRouter(prefix="/memory", tags=["memory"])

@memory_extra.get("/overview")
async def memory_overview(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Overview memory untuk dashboard."""
    l1 = await db.scalar(select(func.count()).select_from(Memory).where(Memory.user_id == current_user.id, Memory.layer == "L1"))
    l2 = await db.scalar(select(func.count()).select_from(Memory).where(Memory.user_id == current_user.id, Memory.layer == "L2"))
    l3 = await db.scalar(select(func.count()).select_from(Memory).where(Memory.user_id == current_user.id, Memory.layer == "L3"))
    return {
        "layers": {
            "L1": {"count": l1 or 0},
            "L2": {"count": l2 or 0},
            "L3": {"count": l3 or 0},
        },
        "surfaces": [],
        "recent": [],
    }

@memory_extra.get("/settings")
async def memory_settings(
    current_user: User = Depends(get_current_user),
):
    return {
        "update": {"l2_budget": 8, "l3_budget": 12},
        "audit": {"l2_budget": 4, "l3_budget": 6},
        "dedup": {"iterations": 2, "auto_after_update": True},
    }

@memory_extra.put("/settings")
async def update_memory_settings(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    body = await request.json()
    return body or {
        "update": {"l2_budget": 8, "l3_budget": 12},
        "audit": {"l2_budget": 4, "l3_budget": 6},
        "dedup": {"iterations": 2, "auto_after_update": True},
    }

@memory_extra.get("/snapshot/{surface}")
async def memory_snapshot(
    surface: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.scalars(
        select(Memory).where(Memory.user_id == current_user.id, Memory.surface == surface).order_by(Memory.created_at.desc()).limit(20)
    )
    entities = [{"id": str(m.id), "layer": m.layer, "content": m.content, "label": m.content[:50]} for m in result.all()]
    return {"entities": entities}

@memory_extra.get("/snapshot/{surface}/changes")
async def memory_snapshot_changes(
    surface: str,
    current_user: User = Depends(get_current_user),
):
    return {"surface": surface, "changes": []}

@memory_extra.post("/snapshot/{surface}/refresh")
async def memory_snapshot_refresh(
    surface: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok", "surface": surface}

@memory_extra.get("/doc/{layer}/{doc_key}")
async def memory_doc(
    layer: str,
    doc_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m = await db.scalar(
        select(Memory).where(Memory.user_id == current_user.id, Memory.layer == layer).order_by(Memory.created_at.desc())
    )
    if not m:
        return {"content": ""}
    return {"content": m.content}

@memory_extra.get("/doc/{layer}/{doc_key}/lines")
async def memory_doc_lines(
    layer: str,
    doc_key: str,
    current_user: User = Depends(get_current_user),
):
    return {"lines": []}

@memory_extra.post("/doc/{layer}/{doc_key}/update")
async def memory_doc_update(
    layer: str,
    doc_key: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@memory_extra.post("/doc/{layer}/{doc_key}/reset")
async def memory_doc_reset(
    layer: str,
    doc_key: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@memory_extra.get("/trace/kb")
async def memory_trace_kb(
    current_user: User = Depends(get_current_user),
):
    return {"traces": []}

@memory_extra.get("/resolve_entry/{entry_id}")
async def memory_resolve_entry(
    entry_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"id": entry_id, "resolved": False, "content": ""}

# Memory runs
@memory_extra.get("/runs")
async def memory_runs(
    layer: str | None = None,
    key: str | None = None,
    current_user: User = Depends(get_current_user),
):
    return {"runs": []}

@memory_extra.post("/runs/start")
async def memory_run_start(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"run_id": str(uuid.uuid4()), "status": "started"}

@memory_extra.get("/runs/{run_id}")
async def memory_run_detail(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"run_id": run_id, "status": "completed", "result": {}}

@memory_extra.get("/runs/{run_id}/events")
async def memory_run_events(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"events": []}

@memory_extra.post("/runs/{run_id}/cancel")
async def memory_run_cancel(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "cancelled", "run_id": run_id}

@memory_extra.post("/runs/{run_id}/undo")
async def memory_run_undo(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok", "run_id": run_id}


# ── Book stubs ──────────────────────────────────────────────────────────
book_router = APIRouter(prefix="/book", tags=["book"])

@book_router.get("/books")
async def list_books(
    current_user: User = Depends(get_current_user),
):
    return {"books": []}

@book_router.post("/books")
async def create_book(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    book_id = str(uuid.uuid4())
    try:
        body = await request.json()
        intent = body.get("user_intent", "") or "Untitled Book"
    except Exception:
        intent = "Untitled Book"
    return {
        "book": {
            "id": book_id,
            "title": intent[:50] if intent else "Untitled Book",
            "description": intent,
            "status": "draft",
            "proposal": None,
            "knowledge_bases": [],
            "language": "id",
            "page_count": 0,
            "chapter_count": 0,
            "created_at": 0,
            "updated_at": 0,
            "metadata": {},
        },
        "proposal": {
            "title": intent[:80] if intent else "Untitled Book",
            "description": intent,
            "scope": "",
            "target_level": "beginner",
            "estimated_chapters": 5,
            "rationale": "",
        },
    }


# ── Learning progress stubs ─────────────────────────────────────────────
learning_router = APIRouter(prefix="/learning", tags=["learning"])

@learning_router.get("/progress")
async def learning_progress_list(
    current_user: User = Depends(get_current_user),
):
    return {"items": [], "total": 0}

@learning_router.get("/progress/{book_id}")
async def learning_progress_detail(
    book_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"book_id": book_id, "modules": [], "mastery_levels": {}, "current_module_id": None, "current_stage": "diagnostic"}

@learning_router.post("/progress/{book_id}/init-modules")
async def learning_init_modules(
    book_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok", "book_id": book_id}

@learning_router.get("/progress/{path_id}/map")
async def learning_mastery_map(
    path_id: str,
    current_user: User = Depends(get_current_user),
):
    return {
        "book_id": path_id,
        "next": {"action": "diagnostic", "knowledge_point_name": "", "knowledge_point_type": "", "status": "new", "mastery": 0, "threshold": 0.8, "reason": ""},
        "map": {"counts": {"mastered": 0, "learning": 0, "new": 0, "total": 0}, "due_reviews": 0, "complete": False, "modules": []},
    }

@learning_router.post("/progress/{book_id}/redo")
async def learning_progress_redo(
    book_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok", "book_id": book_id}

@learning_router.post("/progress/{book_id}/import-from-book")
async def learning_import_from_book(
    book_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok", "book_id": book_id}

@learning_router.post("/progress/{book_id}/generate-from-notebook")
async def learning_generate_from_notebook(
    book_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok", "book_id": book_id}


# ── Imports stubs ───────────────────────────────────────────────────────
imports_router = APIRouter(prefix="/imports", tags=["imports"])

class ImportPayload(BaseModel):
    source: dict[str, Any] | None = None
    sessions: list[dict[str, Any]] = []
    agent_id: str = ""
    agent_name: str = ""

@imports_router.post("/chat-history")
async def import_chat_history(
    payload: ImportPayload,
    current_user: User = Depends(get_current_user),
):
    return {"imported": 0, "skipped": len(payload.sessions), "sessions": []}

@imports_router.get("/chat-history")
async def list_imported_sessions(
    limit: int = 200,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
):
    return {"sessions": [], "total": 0}


# ── Subagents extra stubs ───────────────────────────────────────────────
subagents_router = APIRouter(prefix="/subagents", tags=["subagents"])

@subagents_router.get("/detect")
async def subagents_detect(
    current_user: User = Depends(get_current_user),
):
    return {"detected": [], "available": []}

@subagents_router.get("/backends/options")
async def subagents_backend_options(
    current_user: User = Depends(get_current_user),
):
    return {"backends": []}

@subagents_router.post("/backends/{kind}/sync")
async def subagents_backend_sync(
    kind: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok", "kind": kind}

@subagents_router.post("/connections/{name}/message")
async def subagents_send_message(
    name: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok", "connection": name}

@subagents_router.delete("/connections/{name}")
async def subagents_delete_connection(
    name: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@subagents_router.get("/partners")
async def subagents_partners(
    current_user: User = Depends(get_current_user),
):
    return {"partners": []}


# ── Plugins stubs ───────────────────────────────────────────────────────
plugins_router = APIRouter(prefix="/plugins", tags=["plugins"])

@plugins_router.get("/list")
async def plugins_list(
    current_user: User = Depends(get_current_user),
):
    return {"plugins": [], "tools": []}

@plugins_router.post("/tools/{tool_name}/execute-stream")
async def plugin_tool_execute_stream(
    tool_name: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    from fastapi.responses import StreamingResponse
    async def stream():
        yield 'data: {"status":"error","message":"Plugin execution not implemented"}\n\n'
    return StreamingResponse(stream(), media_type="text/event-stream")

@plugins_router.post("/capabilities/{capability_name}/execute-stream")
async def plugin_capability_execute_stream(
    capability_name: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    from fastapi.responses import StreamingResponse
    async def stream():
        yield 'data: {"status":"error","message":"Capability not implemented"}\n\n'
    return StreamingResponse(stream(), media_type="text/event-stream")


# ── Voice stubs ─────────────────────────────────────────────────────────
voice_router = APIRouter(prefix="/voice", tags=["voice"])

@voice_router.post("/stt")
async def voice_stt(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"text": "", "status": "not_configured"}

@voice_router.post("/tts")
async def voice_tts(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"audio_url": "", "status": "not_configured"}


# ── Skills hub stubs ────────────────────────────────────────────────────
skills_hub_router = APIRouter(prefix="/skills", tags=["skills"])

@skills_hub_router.get("/hub/catalog")
async def skills_hub_catalog(
    q: str | None = None,
    current_user: User = Depends(get_current_user),
):
    return {"skills": [], "categories": []}

@skills_hub_router.get("/hub/detail")
async def skills_hub_detail(
    name: str = "",
    current_user: User = Depends(get_current_user),
):
    return {"name": name, "description": "", "content": "", "found": False}

@skills_hub_router.post("/install")
async def skills_install(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok", "skill_id": str(uuid.uuid4())}

@skills_hub_router.get("/tags/list")
async def skills_tags_list(
    current_user: User = Depends(get_current_user),
):
    return {"tags": []}

@skills_hub_router.post("/tags/create")
async def skills_tag_create(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok", "tag_id": str(uuid.uuid4())}

@skills_hub_router.put("/tags/{name}")
async def skills_tag_update(
    name: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@skills_hub_router.delete("/tags/{name}")
async def skills_tag_delete(
    name: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}


# ── Notebook (file-backed) extra endpoints ──────────────────────────────
notebook_extra = APIRouter(prefix="/notebook", tags=["notebook"])

@notebook_extra.get("/list")
async def notebook_list(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.scalars(
        select(Notebook).where(Notebook.user_id == current_user.id).order_by(Notebook.updated_at.desc())
    )
    notebooks = result.all()
    return {
        "notebooks": [
            {
                "id": str(n.id),
                "name": n.title,
                "description": "",
                "color": "#6366F1",
                "icon": "book",
                "record_count": 0,
                "created_at": int(n.created_at.timestamp()) if n.created_at else 0,
                "updated_at": int(n.updated_at.timestamp()) if n.updated_at else 0,
            }
            for n in notebooks
        ]
    }

class NotebookCreateReq(BaseModel):
    name: str
    description: str = ""
    color: str = "#6366F1"
    icon: str = "book"

@notebook_extra.post("/create")
async def notebook_create(
    payload: NotebookCreateReq,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    nb = Notebook(
        user_id=current_user.id,
        title=payload.name,
        content=payload.description,
    )
    db.add(nb)
    await db.commit()
    await db.refresh(nb)
    return {
        "notebook": {
            "id": str(nb.id),
            "name": nb.title,
            "description": nb.content,
            "color": payload.color,
            "icon": payload.icon,
            "record_count": 0,
            "created_at": int(nb.created_at.timestamp()) if nb.created_at else 0,
            "updated_at": int(nb.updated_at.timestamp()) if nb.updated_at else 0,
        }
    }

@notebook_extra.get("/{notebook_id}")
async def notebook_get(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        nb_id = uuid.UUID(notebook_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid notebook ID")
    nb = await db.scalar(select(Notebook).where(Notebook.id == nb_id, Notebook.user_id == current_user.id))
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook tidak ditemukan")
    return {
        "id": str(nb.id),
        "name": nb.title,
        "description": nb.content,
        "color": "#6366F1",
        "icon": "book",
        "records": [],
        "created_at": int(nb.created_at.timestamp()) if nb.created_at else 0,
        "updated_at": int(nb.updated_at.timestamp()) if nb.updated_at else 0,
    }

class NotebookUpdateReq(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None
    icon: str | None = None

@notebook_extra.put("/{notebook_id}")
async def notebook_update(
    notebook_id: str,
    payload: NotebookUpdateReq,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        nb_id = uuid.UUID(notebook_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid notebook ID")
    nb = await db.scalar(select(Notebook).where(Notebook.id == nb_id, Notebook.user_id == current_user.id))
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook tidak ditemukan")
    if payload.name is not None:
        nb.title = payload.name
    if payload.description is not None:
        nb.content = payload.description
    await db.commit()
    await db.refresh(nb)
    return {
        "notebook": {
            "id": str(nb.id),
            "name": nb.title,
            "description": nb.content,
            "color": "#6366F1",
            "icon": "book",
            "record_count": 0,
            "created_at": int(nb.created_at.timestamp()) if nb.created_at else 0,
            "updated_at": int(nb.updated_at.timestamp()) if nb.updated_at else 0,
        }
    }

@notebook_extra.delete("/{notebook_id}")
async def notebook_delete(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        nb_id = uuid.UUID(notebook_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid notebook ID")
    nb = await db.scalar(select(Notebook).where(Notebook.id == nb_id, Notebook.user_id == current_user.id))
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook tidak ditemukan")
    await db.delete(nb)
    await db.commit()
    return {"status": "ok"}

@notebook_extra.get("/{notebook_id}/records/{record_id}")
async def notebook_get_record(
    notebook_id: str,
    record_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"record": None, "found": False}

class AddRecordReq(BaseModel):
    type: str = "chat"
    title: str = ""
    summary: str = ""
    user_query: str = ""
    output: str = ""
    metadata: dict[str, Any] | None = None
    kb_name: str | None = None

@notebook_extra.post("/add_record_with_summary")
async def notebook_add_record(
    payload: AddRecordReq,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return {"status": "ok", "record_id": str(uuid.uuid4())}


# ── Auth extra endpoints ────────────────────────────────────────────────
auth_extra = APIRouter(prefix="/auth", tags=["auth"])

@auth_extra.get("/me")
async def auth_me(
    current_user: User = Depends(get_current_user),
):
    return {
        "id": str(current_user.id),
        "username": current_user.username,
        "full_name": getattr(current_user, "full_name", "") or "",
        "email": getattr(current_user, "email", "") or "",
        "is_admin": getattr(current_user, "is_admin", False),
    }

@auth_extra.get("/profile")
async def auth_profile(
    current_user: User = Depends(get_current_user),
):
    return {
        "id": str(current_user.id),
        "username": current_user.username,
        "full_name": getattr(current_user, "full_name", "") or "",
        "email": getattr(current_user, "email", "") or "",
        "avatar": None,
    }

class ProfileUpdateReq(BaseModel):
    full_name: str | None = None
    email: str | None = None

@auth_extra.put("/profile")
async def auth_update_profile(
    payload: ProfileUpdateReq,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.full_name is not None:
        current_user.full_name = payload.full_name
    if payload.email is not None:
        current_user.email = payload.email
    await db.commit()
    return {
        "id": str(current_user.id),
        "username": current_user.username,
        "full_name": current_user.full_name or "",
        "email": getattr(current_user, "email", "") or "",
    }

@auth_extra.get("/avatar/{user_id}")
async def auth_avatar(
    user_id: str,
):
    raise HTTPException(status_code=404, detail="No avatar")

@auth_extra.post("/profile/avatar")
async def auth_upload_avatar(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok", "avatar_url": ""}

@auth_extra.get("/users")
async def auth_list_users(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")
    result = await db.scalars(select(User).order_by(User.created_at.desc()))
    return {"users": [{"id": str(u.id), "username": u.username, "full_name": getattr(u, "full_name", "") or "", "is_admin": getattr(u, "is_admin", False)} for u in result.all()]}

@auth_extra.delete("/users/{username}")
async def auth_delete_user(
    username: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")
    u = await db.scalar(select(User).where(User.username == username))
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(u)
    await db.commit()
    return {"status": "ok"}

class UserRoleReq(BaseModel):
    is_admin: bool = False

@auth_extra.put("/users/{username}/role")
async def auth_update_user_role(
    username: str,
    payload: UserRoleReq,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")
    u = await db.scalar(select(User).where(User.username == username))
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    u.is_admin = payload.is_admin
    await db.commit()
    return {"status": "ok"}


# ── Settings extra stubs ────────────────────────────────────────────────
settings_extra = APIRouter(tags=["settings"])

@settings_extra.get("/settings/voice-autoplay")
async def settings_voice_autoplay(
    current_user: User = Depends(get_current_user),
):
    return {"enabled": False}

@settings_extra.put("/settings/voice-autoplay")
async def settings_update_voice_autoplay(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"enabled": False}

@settings_extra.get("/settings/enabled-tools")
async def settings_enabled_tools(
    current_user: User = Depends(get_current_user),
):
    return {"tools": []}

@settings_extra.put("/settings/enabled-tools")
async def settings_update_enabled_tools(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"tools": []}

@settings_extra.get("/settings/mcp")
async def settings_mcp(
    current_user: User = Depends(get_current_user),
):
    return {"servers": [], "enabled": False}

@settings_extra.put("/settings/mcp")
async def settings_update_mcp(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"servers": []}

@settings_extra.post("/settings/providers/openai-codex")
async def settings_codex_oauth(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "not_configured"}

@settings_extra.get("/settings/providers/openai-codex")
async def settings_codex_get(
    current_user: User = Depends(get_current_user),
):
    return {"status": "not_configured", "connected": False}

# Codex OAuth sub-routes (FE calls /oauth/status, /oauth/start, dll.)
@settings_extra.get("/settings/providers/openai-codex/oauth/status")
async def codex_oauth_status(
    current_user: User = Depends(get_current_user),
):
    return {"status": "not_configured", "connected": False}

@settings_extra.post("/settings/providers/openai-codex/oauth/start")
async def codex_oauth_start(
    current_user: User = Depends(get_current_user),
):
    return {"status": "not_configured"}

@settings_extra.post("/settings/providers/openai-codex/oauth/cancel")
async def codex_oauth_cancel(
    current_user: User = Depends(get_current_user),
):
    return {"status": "not_configured"}

@settings_extra.post("/settings/providers/openai-codex/oauth/logout")
async def codex_oauth_logout(
    current_user: User = Depends(get_current_user),
):
    return {"status": "not_configured"}

@settings_extra.post("/settings/providers/openai-codex/models/refresh")
async def codex_models_refresh(
    current_user: User = Depends(get_current_user),
):
    return {"status": "not_configured"}

# Document-parsing download status (GET)
@settings_extra.get("/settings/document-parsing/models/download/status")
async def doc_parsing_download_status(
    current_user: User = Depends(get_current_user),
):
    return {"status": "idle", "progress": 0}

# Mineru download status (GET)
@settings_extra.get("/settings/mineru/models/download/status")
async def mineru_download_status(
    current_user: User = Depends(get_current_user),
):
    return {"status": "idle", "progress": 0}

# Voice GET endpoints (FE may poll with GET)
@voice_router.get("/stt")
async def voice_stt_info(
    current_user: User = Depends(get_current_user),
):
    return {"status": "not_configured"}

@voice_router.get("/tts")
async def voice_tts_info(
    current_user: User = Depends(get_current_user),
):
    return {"status": "not_configured"}

# Mineru test GET (FE settings page may poll)
@settings_extra.get("/settings/mineru/test")
async def mineru_test_info(
    current_user: User = Depends(get_current_user),
):
    return {"status": "not_configured"}

# Mineru models download GET
@settings_extra.get("/settings/mineru/models/download")
async def mineru_download_info(
    current_user: User = Depends(get_current_user),
):
    return {"status": "not_configured"}


# ── Knowledge extra stubs ───────────────────────────────────────────────
knowledge_extra = APIRouter(prefix="/knowledge", tags=["knowledge"])

@knowledge_extra.post("/create")
async def knowledge_create(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"kb_id": str(uuid.uuid4()), "status": "created"}

# Batas berkas .md yang diimpor sekali jalan — vault besar tidak boleh
# menggantung request. Overflow dilaporkan (jangan diam-diam dipotong).
_MAKS_MD_VAULT = 200


def _pindai_vault_md(root: Path) -> tuple[list[dict[str, str]], int]:
    """Pindai `*.md` di vault (rekursif). Kembalikan (daftar berkas, total ditemukan).

    Judul diambil dari heading `#` pertama; jika tidak ada, pakai nama berkas.
    Isi TIDAK dibaca penuh di sini — hanya beberapa baris awal untuk judul; isi
    lengkap dibaca on-demand oleh `ref_read`. Berjalan di thread (I/O blocking).
    """
    semua = sorted(p for p in root.rglob("*.md") if p.is_file())
    total = len(semua)
    hasil: list[dict[str, str]] = []
    for p in semua[:_MAKS_MD_VAULT]:
        judul = p.stem
        try:
            with p.open("r", encoding="utf-8", errors="ignore") as f:
                for _ in range(60):  # cukup untuk lewati front-matter YAML
                    baris = f.readline()
                    if not baris:
                        break
                    s = baris.strip()
                    if s.startswith("#"):
                        judul = s.lstrip("#").strip()[:300] or p.stem
                        break
        except OSError:
            pass
        hasil.append(
            {"path": str(p.resolve()), "filename": p.name[:500], "title": judul[:1000]}
        )
    return hasil, total


@knowledge_extra.post("/connect-obsidian")
async def knowledge_connect_obsidian(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Impor vault Obsidian sebagai referensi jurnal — sumber pengetahuan agent.

    Vault = folder catatan Markdown milik pengguna. Tiap `.md` didaftarkan sebagai
    satu `JournalReference` (pola sama seperti `cite_add`) dengan `file_path` = path
    absolut berkas; isi TIDAK disalin — `ref_read` membacanya on-demand. Dengan
    begitu catatan vault otomatis muncul di `cite_list` dan bisa dibaca agent saat
    grounding/brainstorm TANPA tool atau model embedding baru (model-agnostik),
    langsung menyambung ke alur referensi/sitasi jurnal.

    Impor bersifat MENAMBAH & idempoten: berkas yang `file_path`-nya sudah terdaftar
    dilewati (menghormati "jangan hapus data"). Body: `{name, vault_path}`.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    nama = (str(payload.get("name") or "").strip() or "Vault Obsidian")[:255]
    vault_path = str(payload.get("vault_path") or "").strip()
    if not vault_path:
        raise HTTPException(status_code=400, detail="Path vault kosong.")

    root = Path(vault_path).expanduser()
    if not root.exists() or not root.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Folder vault tidak ditemukan atau bukan folder: {vault_path}",
        )

    berkas, total = await asyncio.to_thread(_pindai_vault_md, root)
    root_abs = str(root.resolve())
    if not berkas:
        return {
            "status": "ok",
            "name": nama,
            "vault_path": root_abs,
            "imported": 0,
            "skipped": 0,
            "total_ditemukan": total,
            "message": "Tidak ada berkas .md di folder itu.",
        }

    # Grup terpisah per-vault agar mudah ditelusuri & (kelak) dihapus per-vault.
    grup_nama = f"Vault: {nama}"[:255]
    grup = await db.scalar(
        select(JournalGroup).where(
            JournalGroup.user_id == current_user.id,
            JournalGroup.name == grup_nama,
        )
    )
    if grup is None:
        grup = JournalGroup(
            user_id=current_user.id,
            name=grup_nama,
            description=(
                f"Catatan Obsidian dari vault '{nama}'. Sumber pengetahuan yang "
                "dibaca agent Co-Writer saat menyusun & membrainstorm draf."
            ),
        )
        db.add(grup)
        await db.flush()

    # Dedup berdasarkan file_path absolut → impor ulang vault yang sama aman.
    terpakai = set(
        await db.scalars(
            select(JournalReference.file_path).where(
                JournalReference.user_id == current_user.id
            )
        )
    )
    imported = 0
    skipped = 0
    for b in berkas:
        if b["path"] in terpakai:
            skipped += 1
            continue
        db.add(
            JournalReference(
                user_id=current_user.id,
                group_id=grup.id,
                filename=b["filename"],
                file_path=b["path"],
                title=b["title"] or b["filename"],
                authors=None,
                year=None,
                journal_name=f"Catatan Obsidian — {nama}"[:1000],
                status="extracted",
            )
        )
        terpakai.add(b["path"])
        imported += 1

    await db.commit()
    if total > len(berkas):
        logger.info(
            "connect-obsidian: vault '%s' punya %d berkas .md, hanya %d diimpor "
            "(batas %d). Sisanya belum diimpor.",
            nama, total, len(berkas), _MAKS_MD_VAULT,
        )
    logger.info(
        "connect-obsidian: user %s impor vault '%s' → %d baru, %d dilewati.",
        current_user.id, nama, imported, skipped,
    )
    return {
        "status": "ok",
        "name": nama,
        "vault_path": root_abs,
        "imported": imported,
        "skipped": skipped,
        "total_ditemukan": total,
    }

@knowledge_extra.post("/probe-folder")
async def knowledge_probe_folder(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"valid": False, "files": 0}

@knowledge_extra.post("/connect-folder")
async def knowledge_connect_folder(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "error", "message": "Folder connection not implemented"}

@knowledge_extra.post("/probe-lightrag-server")
async def knowledge_probe_lightrag(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"valid": False}

@knowledge_extra.post("/connect-lightrag-server")
async def knowledge_connect_lightrag(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "error", "message": "LightRAG connection not implemented"}

@knowledge_extra.get("/rag-providers")
async def knowledge_rag_providers(
    current_user: User = Depends(get_current_user),
):
    return {"providers": []}

@knowledge_extra.get("/supported-file-types")
async def knowledge_supported_file_types(
    current_user: User = Depends(get_current_user),
):
    return {"extensions": [".pdf", ".txt", ".md", ".docx", ".doc", ".pptx", ".xlsx", ".csv", ".html"]}

@knowledge_extra.get("/rag-pipelines/active-model")
async def knowledge_rag_active_model(
    current_user: User = Depends(get_current_user),
):
    return {"model": "", "provider": ""}

@knowledge_extra.get("/rag-pipelines/model-options")
async def knowledge_rag_model_options(
    current_user: User = Depends(get_current_user),
):
    return {"models": []}

@knowledge_extra.get("/rag-pipelines/{provider}/config")
async def knowledge_rag_pipeline_config(
    provider: str,
    current_user: User = Depends(get_current_user),
):
    return {"provider": provider, "config": {}}

@knowledge_extra.put("/rag-pipelines/{provider}/config")
async def knowledge_rag_update_pipeline_config(
    provider: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@knowledge_extra.post("/rag-pipelines/{provider}/preflight")
async def knowledge_rag_preflight(
    provider: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"ok": True, "warnings": []}

@knowledge_extra.put("/rag-providers/{provider}/mode")
async def knowledge_rag_provider_mode(
    provider: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@knowledge_extra.get("/{name}/files")
async def knowledge_list_files(
    name: str,
    current_user: User = Depends(get_current_user),
):
    return {"files": []}

@knowledge_extra.post("/{name}/upload")
async def knowledge_upload_file(
    name: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@knowledge_extra.get("/{name}/folders")
async def knowledge_list_folders(
    name: str,
    current_user: User = Depends(get_current_user),
):
    return {"folders": []}

@knowledge_extra.post("/{name}/files/move")
async def knowledge_move_file(
    name: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@knowledge_extra.post("/default/{name}")
async def knowledge_set_default(
    name: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@knowledge_extra.post("/{name}/reindex")
async def knowledge_reindex(
    name: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@knowledge_extra.post("/{name}/retry")
async def knowledge_retry(
    name: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@knowledge_extra.get("/{name}/file-preview-text/{filename:path}")
async def knowledge_file_preview_text(
    name: str,
    filename: str,
    current_user: User = Depends(get_current_user),
):
    return {"text": "", "filename": filename}


# ── Chat session extras ─────────────────────────────────────────────────
chat_extra = APIRouter(prefix="/chat", tags=["chat"])

@chat_extra.get("/sessions/{session_id}/quiz-results")
async def chat_quiz_results(
    session_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"results": []}

@chat_extra.put("/sessions/{session_id}/branch-selection")
async def chat_branch_selection(
    session_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@chat_extra.delete("/sessions/{session_id}/messages/{message_id}")
async def chat_delete_message(
    session_id: str,
    message_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}


# ── Multi-user stubs ────────────────────────────────────────────────────
multiuser_router = APIRouter(prefix="/multi-user", tags=["multi-user"])

@multiuser_router.get("/admin/resources")
async def multiuser_resources(
    current_user: User = Depends(get_current_user),
):
    return {"resources": []}

@multiuser_router.get("/users/{user_id}/grants")
async def multiuser_user_grants(
    user_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"grants": []}


# ── Questions extras (for question-notebook) ────────────────────────────
questions_extra = APIRouter(prefix="/questions", tags=["questions"])

@questions_extra.get("/categories")
async def question_categories(
    current_user: User = Depends(get_current_user),
):
    return []

@questions_extra.post("/categories")
async def question_create_category(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"category_id": str(uuid.uuid4())}

@questions_extra.get("/categories/{category_id}")
async def question_get_category(
    category_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"id": category_id, "name": "", "entries": []}

@questions_extra.delete("/categories/{category_id}")
async def question_delete_category(
    category_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@questions_extra.get("/entries")
async def question_entries(
    category_id: int | None = None,
    bookmarked: bool | None = None,
    is_correct: bool | None = None,
    limit: int = 200,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
):
    return {"items": [], "total": 0}

@questions_extra.post("/entries/upsert")
async def question_entry_upsert(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"entry_id": str(uuid.uuid4())}

@questions_extra.get("/entries/lookup/by-question")
async def question_entry_lookup(
    question_id: str = "",
    current_user: User = Depends(get_current_user),
):
    return {"entry": None, "found": False}

@questions_extra.get("/entries/{entry_id}")
async def question_entry_detail(
    entry_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"entry": None, "found": False}

@questions_extra.put("/entries/{entry_id}")
async def question_entry_update(
    entry_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@questions_extra.delete("/entries/{entry_id}")
async def question_entry_delete(
    entry_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@questions_extra.post("/entries/{entry_id}/categories")
async def question_entry_add_category(
    entry_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}

@questions_extra.delete("/entries/{entry_id}/categories/{category_id}")
async def question_entry_remove_category(
    entry_id: str,
    category_id: str,
    current_user: User = Depends(get_current_user),
):
    return {"status": "ok"}


# ── WebSocket quiz judge ────────────────────────────────────────────────
# /api/v1/question/judge WS endpoint is handled by ws_chat or a dedicated router.
# For now, provide a 404-safe HTTP fallback so FE doesn't crash.
quiz_judge_router = APIRouter(prefix="/question", tags=["quiz"])

@quiz_judge_router.get("/judge")
async def quiz_judge_info():
    return {"endpoint": "WebSocket", "path": "/api/v1/question/judge"}


# ── Settings/embedding stubs ────────────────────────────────────────────
@settings_extra.get("/settings/embedding")
async def settings_embedding(
    current_user: User = Depends(get_current_user),
):
    return {"model": "", "provider": "", "configured": False}
