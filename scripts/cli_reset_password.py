import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.user import User
from app.core.security import hash_password

async def reset_password(email: str, new_password: str):
    async with AsyncSessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        if not user:
            print(f"Error: User dengan email {email} tidak ditemukan di database.")
            return

        user.hashed_password = hash_password(new_password)
        await db.commit()
        print(f"Berhasil mereset kata sandi untuk akun: {email}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Penggunaan: python cli_reset_password.py <email> <new_password>")
        sys.exit(1)
    
    email = sys.argv[1]
    new_password = sys.argv[2]
    
    asyncio.run(reset_password(email, new_password))
