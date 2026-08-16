"""Entry point aplikasi FastAPI Nalar AI."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_optional_user
from app.api.routes.auth import router as auth_router
from app.api.routes.chat import router as chat_router
from app.api.routes.documents import router as documents_router
from app.api.routes.settings import router as settings_router
from app.api.routes.ui_settings import router as ui_settings_router
from app.api.routes.quiz import router as quiz_router
from app.api.routes.progress import router as progress_router
from app.api.routes.agents import router as agents_router
from app.api.routes.notebooks import router as notebooks_router
from app.api.routes.research import router as research_router
from app.api.routes.preferences import router as preferences_router
from app.api.routes.memory import router as memory_router
from app.api.routes.knowledge import router as knowledge_router
from app.api.routes.questions import router as questions_router
from app.api.routes.skills import router as skills_router
from app.api.routes.visualize import router as visualize_router
from app.api.routes.co_writer import router as co_writer_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.journal import router as journal_router
from app.api.routes.ws_chat import router as ws_chat_router
from app.api.routes.capabilities import router as capabilities_router
from app.api.routes.document_parsing import (
    document_parsing_router,
    mineru_router,
)
from app.api.routes.stub_endpoints import (
    memory_extra,
    book_router,
    learning_router,
    imports_router,
    subagents_router,
    plugins_router,
    voice_router,
    skills_hub_router,
    notebook_extra,
    auth_extra,
    settings_extra,
    knowledge_extra,
    chat_extra,
    multiuser_router,
    questions_extra,
    quiz_judge_router,
)
from app.core.config import settings
from app.db.base import Base
from sqlalchemy import select, func
from app.db.schema_patch import terapkan_kolom_tambahan
from app.db.session import engine, AsyncSessionLocal, get_db
from app.models.model_config import ModelConfig
from app.models.user import User
# Import model agar terdaftar ke Base.metadata (create_all saat startup)
from app.models.co_writer_checkpoint import CoWriterCheckpoint  # noqa: F401
from app.models.co_writer_file import CoWriterFile  # noqa: F401
from app.models.co_writer_folder import CoWriterFolder  # noqa: F401
from app.models.journal import (  # noqa: F401
    CitationCategory,
    JournalGroup,
    JournalReference,
    SavedCitation,
)
from app.core.security import hash_password
from app.services.ui_catalog import active_profile_and_model, read_catalog

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Buat tabel otomatis jika menggunakan SQLite/lokal
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all hanya membuat tabel yang belum ada; ia tidak menambah kolom
        # baru ke tabel yang sudah terpasang. Tanpa langkah ini, kolom yang baru
        # ditambahkan ke model gagal dengan "no such column" di basis data lama.
        await terapkan_kolom_tambahan(conn)


    # Seed default admin user jika belum ada user sama sekali
    async with AsyncSessionLocal() as session:
        user_count = await session.scalar(select(func.count()).select_from(User))
        if user_count == 0:
            default_admin = User(
                username="admin",
                hashed_password=hash_password("CHANGEME"),
                full_name="Administrator",
                is_admin=True,
            )
            session.add(default_admin)
            await session.commit()
    yield

app = FastAPI(title=settings.APP_NAME, docs_url="/docs", redoc_url="/redoc", lifespan=lifespan)

# Media hasil import dokumen (gambar PDF/DOCX) di uploads/ — dilayani statis.
import os as _os

app.mount(
    "/uploads",
    StaticFiles(directory=_os.path.join(_os.getcwd(), "uploads")),
    name="uploads",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix=settings.API_PREFIX)
app.include_router(settings_router, prefix=settings.API_PREFIX)
app.include_router(ui_settings_router, prefix=settings.API_PREFIX)
app.include_router(documents_router, prefix=settings.API_PREFIX)
app.include_router(chat_router, prefix=settings.API_PREFIX)
app.include_router(quiz_router, prefix=settings.API_PREFIX)
app.include_router(progress_router, prefix=settings.API_PREFIX)
app.include_router(agents_router, prefix=settings.API_PREFIX)
app.include_router(notebooks_router, prefix=settings.API_PREFIX)
app.include_router(research_router, prefix=settings.API_PREFIX)
app.include_router(preferences_router, prefix=settings.API_PREFIX)
app.include_router(memory_router, prefix=settings.API_PREFIX)
# Stub endpoints DULU sebelum knowledge_router agar route seperti /rag-providers
# tidak tertangkap oleh /{kb_id} (UUID) di knowledge_router.
app.include_router(knowledge_extra, prefix=settings.API_PREFIX)
app.include_router(knowledge_router, prefix=settings.API_PREFIX)
app.include_router(questions_extra, prefix=settings.API_PREFIX)
app.include_router(questions_router, prefix=settings.API_PREFIX)
app.include_router(skills_router, prefix=settings.API_PREFIX)
app.include_router(visualize_router, prefix=settings.API_PREFIX)
app.include_router(co_writer_router, prefix=settings.API_PREFIX)
app.include_router(dashboard_router, prefix=settings.API_PREFIX)
app.include_router(journal_router, prefix=settings.API_PREFIX)
app.include_router(document_parsing_router, prefix=settings.API_PREFIX)
app.include_router(mineru_router, prefix=settings.API_PREFIX)
app.include_router(capabilities_router, prefix=settings.API_PREFIX)
# Chat di web berjalan lewat WebSocket ChatOrchestrator, bukan POST /chat.
app.include_router(ws_chat_router, prefix=settings.API_PREFIX)
# Stub endpoints untuk cover FE calls yang missing
app.include_router(memory_extra, prefix=settings.API_PREFIX)
app.include_router(book_router, prefix=settings.API_PREFIX)
app.include_router(learning_router, prefix=settings.API_PREFIX)
app.include_router(imports_router, prefix=settings.API_PREFIX)
app.include_router(subagents_router, prefix=settings.API_PREFIX)
app.include_router(plugins_router, prefix=settings.API_PREFIX)
app.include_router(voice_router, prefix=settings.API_PREFIX)
app.include_router(skills_hub_router, prefix=settings.API_PREFIX)
app.include_router(notebook_extra, prefix=settings.API_PREFIX)
app.include_router(auth_extra, prefix=settings.API_PREFIX)
app.include_router(settings_extra, prefix=settings.API_PREFIX)
app.include_router(chat_extra, prefix=settings.API_PREFIX)
app.include_router(multiuser_router, prefix=settings.API_PREFIX)
app.include_router(quiz_judge_router, prefix=settings.API_PREFIX)


@app.get(f"{settings.API_PREFIX}/health", tags=["health"])
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get(f"{settings.API_PREFIX}/system/status", tags=["system"])
async def system_status(
    current_user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Status layanan untuk badge di halaman Pengaturan.

    Selalu dibatasi pada milik user yang login; sebelumnya baris `is_active`
    milik user lain ikut terhitung sehingga layanan bisa tampak "online"
    padahal user ini belum mengatur apa pun.
    """
    llm_configured = False
    emb_configured = False
    search_configured = False
    llm_model = ""
    emb_model = ""
    search_provider = ""

    if current_user is not None:
        catalog = await read_catalog(db, current_user.id)

        _, llm_model_entry = active_profile_and_model(catalog, "llm")
        llm_model = ((llm_model_entry or {}).get("model") or "").strip()
        llm_configured = bool(llm_model)

        _, emb_model_entry = active_profile_and_model(catalog, "embedding")
        emb_model = ((emb_model_entry or {}).get("model") or "").strip()
        emb_configured = bool(emb_model)

        search_profile, _ = active_profile_and_model(catalog, "search")
        if search_profile is None:
            search_service = catalog.get("services", {}).get("search") or {}
            search_profile = next(
                (
                    p
                    for p in search_service.get("profiles", [])
                    if isinstance(p, dict) and p.get("id") == search_service.get("active_profile_id")
                ),
                None,
            )
        search_provider = ((search_profile or {}).get("provider") or "").strip()
        search_configured = bool(search_provider)

        if not llm_configured:
            # Katalog kosong: user lama yang konfigurasinya hanya ada di model_configs.
            cfg = await db.scalar(
                select(ModelConfig).where(
                    ModelConfig.user_id == current_user.id,
                    ModelConfig.is_active == True,  # noqa: E712 — perbandingan kolom SQLAlchemy
                )
            )
            if cfg is not None:
                llm_configured = True
                llm_model = cfg.model_name
                emb_model = emb_model or cfg.embedding_model
                emb_configured = bool(emb_model)

    return {
        "backend": {"status": "online", "timestamp": None},
        "llm": {"status": "online" if llm_configured else "not_configured", "model": llm_model},
        "embeddings": {
            "status": "online" if emb_configured else "not_configured",
            "model": emb_model,
        },
        "search": {
            "status": "online" if search_configured else "not_configured",
            "provider": search_provider,
        },
    }
