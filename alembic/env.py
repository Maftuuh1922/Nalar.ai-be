"""Konfigurasi Alembic — dijalankan secara sinkron memakai driver psycopg."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.db.base import Base

# Import semua model supaya terdaftar di Base.metadata (dibutuhkan autogenerate)
from app.models import user, model_config, document, chat_history, quiz, chat_session, quiz_attempt, agent, notebook, research_report, user_preference, memory, knowledge_base, question_bank, skill  # noqa: F401

config = context.config

# `postgresql+psycopg://` sudah valid untuk SQLAlchemy sync dengan psycopg3.
# Driver psycopg3 (paket `psycopg`) mendukung mode sync maupun async pada URL
# yang sama. Pastikan DATABASE_URL di .env TIDAK menggunakan suffix `+asyncio`
# (mis. `postgresql+psycopg+asyncio://`) karena itu hanya untuk async engine.
# Jika ada suffix async, strip dulu sebelum diserahkan ke Alembic.
_sync_url = settings.DATABASE_URL.replace("+asyncio", "")
# SQLite dipakai untuk pengembangan lokal dengan driver async `aiosqlite`.
# Alembic berjalan sinkron, jadi turunkan ke driver bawaan `sqlite3`.
_sync_url = _sync_url.replace("sqlite+aiosqlite://", "sqlite://")
config.set_main_option("sqlalchemy.url", _sync_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
