import asyncio
import sys
import os

# Menambahkan direktori root proyek ke sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import engine
from app.db.base import Base

async def reset_db():
    print("Mereset database (menghapus semua data)...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    print("Berhasil mereset database! Memulai dari nol.")

if __name__ == "__main__":
    asyncio.run(reset_db())
