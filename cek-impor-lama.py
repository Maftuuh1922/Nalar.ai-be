"""Uji apakah proxy Next.js (port 3000) putus untuk impor yang lama."""

import asyncio
import os
import time

os.chdir(os.path.dirname(os.path.abspath(__file__)))

import httpx
from sqlalchemy import text as sql

from app.core.security import create_access_token
from app.db.session import AsyncSessionLocal

# Berkas ini pada uji sebelumnya butuh ~123 detik di sisi backend.
BERKAS = os.path.expanduser("~/Downloads/10.4324_9780203761557_previewpdf.pdf")


async def ambil_user():
    async with AsyncSessionLocal() as db:
        r = await db.execute(sql("SELECT id, username FROM users ORDER BY created_at LIMIT 1"))
        return r.fetchone()


uid, uname = asyncio.run(ambil_user())
cookies = {"nalar_token": create_access_token(str(uid))}
nama = os.path.basename(BERKAS)
print(f"user: {uname}   berkas: {nama} ({os.path.getsize(BERKAS) // 1024} KB)\n")

for label, pangkalan in (
    ("proxy 3000   ", "http://localhost:3000"),
    ("langsung 8087", "http://127.0.0.1:8087"),
):
    t0 = time.time()
    try:
        with open(BERKAS, "rb") as fh:
            r = httpx.post(
                f"{pangkalan}/api/v1/co_writer/import-file",
                cookies=cookies,
                files={"file": (nama, fh)},
                timeout=900,
            )
        print(f"{label} {r.status_code} {time.time() - t0:6.1f}s {r.text[:180]}")
    except Exception as exc:  # noqa: BLE001
        print(f"{label} ERR {time.time() - t0:6.1f}s {type(exc).__name__}: {exc}")
