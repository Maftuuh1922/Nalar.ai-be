"""Setup engine & session SQLAlchemy (async) untuk koneksi PostgreSQL."""

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)

if settings.DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _atur_sqlite(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        """Setel SQLite agar tahan permintaan tulis yang tumpang tindih.

        Mode jurnal bawaan mengunci seluruh basis data selama satu penulisan,
        sehingga impor berkas — yang penulisannya berlangsung selama ekstraksi
        DOCX/PDF (puluhan detik) — membuat permintaan lain langsung gagal
        dengan "database is locked" alih-alih menunggu. WAL memisahkan pembaca
        dari penulis, dan `busy_timeout` membuat penulis berikutnya antre
        selama 30 detik sebelum menyerah.
        """
        kursor = dbapi_connection.cursor()
        kursor.execute("PRAGMA journal_mode=WAL")
        kursor.execute("PRAGMA busy_timeout=30000")
        kursor.execute("PRAGMA synchronous=NORMAL")
        kursor.close()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency FastAPI: menyediakan satu session DB per request."""
    async with AsyncSessionLocal() as session:
        yield session
