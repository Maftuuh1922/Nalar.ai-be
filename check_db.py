import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from app.core.config import settings

async def main():
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as session:
        result = await session.execute(text("SELECT base_url, model_name FROM model_configs WHERE is_active=1"))
        for row in result.fetchall():
            print(f"base_url: {row[0]}, model_name: {row[1]}")

asyncio.run(main())
