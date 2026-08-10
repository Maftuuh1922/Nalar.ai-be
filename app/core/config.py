"""Konfigurasi aplikasi, dibaca dari environment variable (atau file .env)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Direktori proyek (Nalar.ai-be) — basis untuk path absolut, tidak bergantung
# pada working directory proses (reloader/spawn uvicorn bisa beda cwd).
_PROJECT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Identitas aplikasi
    APP_NAME: str = "Nalar AI API"
    API_PREFIX: str = "/api/v1"

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///nalar_ai.db"

    # Keamanan / JWT
    SECRET_KEY: str = "changeme-generate-a-random-secret"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    # CORS — daftar origin frontend yang diizinkan, dipisah koma
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001,http://127.0.0.1:3000,http://127.0.0.1:3001"

    # Direktori penyimpanan file upload dan ChromaDB (relatif terhadap working directory)
    UPLOAD_DIR: str = "uploads"
    CHROMA_DIR: str = "chroma_db"

    @property
    def upload_dir_abs(self) -> Path:
        """Path absolut UPLOAD_DIR (anti-cwd-spawn bug)."""
        p = Path(self.UPLOAD_DIR)
        return p if p.is_absolute() else _PROJECT_DIR / p

    @property
    def chroma_dir_abs(self) -> Path:
        p = Path(self.CHROMA_DIR)
        return p if p.is_absolute() else _PROJECT_DIR / p

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
