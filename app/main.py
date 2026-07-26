"""Entry point aplikasi FastAPI Nalar AI."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.auth import router as auth_router
from app.api.routes.chat import router as chat_router
from app.api.routes.documents import router as documents_router
from app.api.routes.settings import router as settings_router
from app.api.routes.quiz import router as quiz_router
from app.api.routes.progress import router as progress_router
from app.api.routes.agents import router as agents_router
from app.api.routes.notebooks import router as notebooks_router
# from app.api.routes.storm import router as storm_router
from app.core.config import settings
from app.db.session import engine
from app.db.base import Base

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Buat tabel otomatis jika menggunakan SQLite/lokal
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(title=settings.APP_NAME, docs_url="/docs", redoc_url="/redoc", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix=settings.API_PREFIX)
app.include_router(settings_router, prefix=settings.API_PREFIX)
app.include_router(documents_router, prefix=settings.API_PREFIX)
app.include_router(chat_router, prefix=settings.API_PREFIX)
app.include_router(quiz_router, prefix=settings.API_PREFIX)
app.include_router(progress_router, prefix=settings.API_PREFIX)
app.include_router(agents_router, prefix=settings.API_PREFIX)
app.include_router(notebooks_router, prefix=settings.API_PREFIX)
# app.include_router(storm_router, prefix=settings.API_PREFIX)


@app.get(f"{settings.API_PREFIX}/health", tags=["health"])
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
