"""Shared test fixtures.

Unit tests need nothing. Integration tests need a throwaway PostgreSQL database:
    TEST_DATABASE_URL=postgresql+asyncpg://xyz:xyz@localhost:5432/xyz_test
Without it, DB fixtures skip the test. The schema is (re)created from models once per run.
"""

import itertools
import os
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio

# Settings must be importable without a real .env.
os.environ.setdefault("BOT_TOKEN", "123456789:TEST-token-TEST-token-TEST-token-123")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-0123456789")
os.environ.setdefault("SUPERADMIN_IDS", "")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database.models import Base, User, UserRole  # noqa: E402

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
_telegram_ids = itertools.count(10_000_001)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_engine() -> AsyncIterator[AsyncEngine]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Session inside an outer transaction that is rolled back after the test.

    `await session.commit()` inside code under test only releases a SAVEPOINT.
    """
    async with db_engine.connect() as conn:
        trans = await conn.begin()
        s = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        try:
            yield s
        finally:
            await s.close()
            await trans.rollback()


@pytest_asyncio.fixture(loop_scope="session")
async def committing_sessionmaker(db_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Real, independently committing sessions (for concurrency tests). Tables are
    truncated after the test."""
    yield async_sessionmaker(db_engine, expire_on_commit=False)
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


MakeUser = Callable[..., Awaitable[User]]


@pytest.fixture
def make_user(session: AsyncSession) -> MakeUser:
    async def _make(role: UserRole = UserRole.STUDENT, **fields: object) -> User:
        user = User(
            telegram_id=fields.pop("telegram_id", next(_telegram_ids)),
            first_name=fields.pop("first_name", "Test"),
            role=role,
            **fields,
        )
        session.add(user)
        await session.flush()
        return user

    return _make
