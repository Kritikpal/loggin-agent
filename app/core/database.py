from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
# expire_on_commit=False: ORM objects stay readable after commit without lazy IO.
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """One session per request. Services commit explicitly; anything uncommitted is rolled back on close."""
    async with SessionLocal() as session:
        yield session
