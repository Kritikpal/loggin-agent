"""Integration test harness. Tests marked `integration` run against an isolated `<app db>_test` PostgreSQL database
that is created and migrated to head on demand; the application database is never touched."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from scripts.create_database import ensure_database

ROOT = Path(__file__).resolve().parents[1]
TABLES = "log_events, source_index_state, projects"


def _test_url() -> str:
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Create the test database if needed and migrate it to head (this also tests the migrations)."""
    url = _test_url()
    assert make_url(url).database.endswith("_test")  # the only database the tests may truncate
    asyncio.run(ensure_database(url))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
    return url


@pytest_asyncio.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(test_database_url)
    async with eng.begin() as conn:  # clean slate for each test (test database only)
        await conn.execute(text(f"TRUNCATE {TABLES}"))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
