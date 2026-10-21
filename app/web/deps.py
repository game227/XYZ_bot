from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request; commit on success, rollback on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
