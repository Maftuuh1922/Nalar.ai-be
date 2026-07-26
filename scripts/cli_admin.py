import asyncio
import sys
import os

# Menambahkan direktori root proyek ke sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.user import User
from app.core.security import hash_password

async def create_admin(email: str, password: str):
    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(User).where(User.email == email))
        if existing:
            print(f"User dengan email {email} sudah ada.")
            return

        user = User(
            email=email,
            hashed_password=hash_password(password),
            full_name="Administrator",
            is_admin=True,
        )
        db.add(user)
        await db.commit()
        print(f"Berhasil membuat akun Admin: {email}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Penggunaan: python cli_admin.py <email> <password>")
        sys.exit(1)
    
    email = sys.argv[1]
    password = sys.argv[2]
    
    asyncio.run(create_admin(email, password))
