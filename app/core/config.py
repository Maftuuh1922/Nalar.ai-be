"""Konfigurasi aplikasi, dibaca dari environment variable (atau file .env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Identitas aplikasi
    APP_NAME: str = "Nalar AI API"
    API_PREFIX: str = "/api"

    # Database
    DATABASE_URL: str = "postgresql+psycopg://nalar:nalar@localhost:5432/nalar_ai"

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
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
