"""Pembacaan preferensi pengguna.

Baris preferensi dibuat otomatis saat pertama kali dibutuhkan sehingga alur
lain cukup memanggil ``get_preferences`` tanpa memeriksa keberadaannya.
"""

import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_preference import UserPreference

# Host yang dianggap lokal saat opsi "lewati proxy untuk alamat lokal" aktif.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "host.docker.internal")


async def get_preferences(db: AsyncSession, user_id: uuid.UUID) -> UserPreference:
    """Ambil preferensi user; buat dengan nilai bawaan bila belum ada."""
    pref = await db.scalar(select(UserPreference).where(UserPreference.user_id == user_id))
    if pref is None:
        pref = UserPreference(user_id=user_id)
        db.add(pref)
        await db.commit()
        await db.refresh(pref)
    return pref


def build_http_client(pref: UserPreference) -> httpx.AsyncClient | None:
    """Buat http client sesuai setelan proxy; ``None`` bila proxy tidak dipakai.

    Mengembalikan ``None`` supaya pemanggil bisa membiarkan SDK memakai client
    bawaannya ketika user tidak mengisi proxy sama sekali.
    """
    proxy = (pref.proxy_url or "").strip()
    if not proxy:
        return None

    mounts: dict[str, httpx.AsyncHTTPTransport | None] = {}
    if pref.bypass_proxy_local:
        # Endpoint lokal (Ollama, LM Studio) tetap diakses langsung.
        for host in _LOCAL_HOSTS:
            mounts[f"all://{host}"] = httpx.AsyncHTTPTransport()
    mounts["all://"] = httpx.AsyncHTTPTransport(proxy=proxy)

    return httpx.AsyncClient(
        mounts=mounts,
        timeout=httpx.Timeout(float(pref.request_timeout)),
    )
