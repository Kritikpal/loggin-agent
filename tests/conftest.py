"""Test harness. Everything runs against an isolated `<app db>_test` database, created and migrated on demand.
The application database is never touched: the guard below refuses to run against any non-`_test` database."""

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.ai.actions import campaign as campaign_action
from app.ai.graphs.checkpointer import open_checkpointer
from app.ai.tools import customers as customers_tool
from app.ai.tools import inventory as inventory_tool
from app.ai.tools import sales as sales_tool
from app.api.v1.ai import chat_model, get_checkpointer
from app.core.database import get_session
from app.main import create_app
from app.models import Inventory, Product, Sale
from scripts.create_database import ensure_database
from tests.scripted_model import ScriptedModel

if sys.platform == "win32":  # psycopg's async mode (the LangGraph checkpointer) cannot run on the default ProactorEventLoop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parents[1]
TABLES = "sales, inventory, products, customers, campaigns, chat_messages"
CHECKPOINT_TABLES = "checkpoints, checkpoint_blobs, checkpoint_writes"


def _test_url() -> str:
    settings = get_settings()
    url = settings.resolved_test_database_url
    name = make_url(url).database or ""
    if not name.endswith("_test") or name == make_url(settings.database_url).database:
        pytest.exit(f"Refusing to run tests against database '{name}': it must end in '_test'", returncode=2)
    return url


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Create the test database if needed and migrate it to head with Alembic (this also tests the migration)."""
    url = _test_url()
    asyncio.run(ensure_database(url))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
    return url


@pytest_asyncio.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    # Pooled (new connections cost ~0.2s each) but wide enough that the 25-request concurrency test gets
    # genuinely parallel connections rather than serialising on the pool.
    eng = create_async_engine(test_database_url, pool_size=5, max_overflow=40)
    async with eng.begin() as conn:  # clean slate for each test (test database only - see _test_url)
        await conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest_asyncio.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.fixture
def tool_session(monkeypatch, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Point the AI tools' get_session at the isolated test database."""

    async def override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s

    for module in (sales_tool, inventory_tool, customers_tool, campaign_action):
        monkeypatch.setattr(module, "get_session", override)


@pytest_asyncio.fixture
async def checkpointer(client: AsyncClient, test_database_url: str):
    """The LangGraph PostgreSQL checkpointer on the isolated test database, wired into the AI endpoints."""
    async with open_checkpointer(test_database_url) as saver:
        async with saver.conn.connection() as conn:
            await conn.execute(f"TRUNCATE {CHECKPOINT_TABLES}")
        client._transport.app.dependency_overrides[get_checkpointer] = lambda: saver
        yield saver


@pytest_asyncio.fixture
async def make_product(session_factory: async_sessionmaker[AsyncSession]):
    """Insert a product + inventory row directly (bypassing the API). Returns the Product."""
    counter = iter(range(1, 10_000))

    async def _make(
        *, category="Jeans", size="32", color="Blue", price="50.00", cost="20.00", quantity=10, reorder_level=5, sku=None
    ) -> Product:
        n = next(counter)
        async with session_factory() as s:
            product = Product(
                name=f"{category} item {n}", category=category, size=size, color=color,
                sku=sku or f"SKU-{uuid.uuid4().hex[:8]}", price=Decimal(price), cost_price=Decimal(cost),
            )
            s.add(product)
            await s.flush()
            s.add(Inventory(product_id=product.id, quantity=quantity, reorder_level=reorder_level))
            await s.commit()
            return product

    return _make


@pytest_asyncio.fixture
async def add_sale(session_factory: async_sessionmaker[AsyncSession]):
    """Insert a historical sale at an exact timestamp (the API always stamps 'now')."""

    async def _add(product: Product, sold_at: datetime, quantity: int = 1) -> None:
        async with session_factory() as s:
            s.add(Sale(
                product_id=product.id, quantity=quantity, unit_price=product.price,
                total_amount=product.price * quantity, sold_at=sold_at.astimezone(UTC),
            ))
            await s.commit()

    return _add


@pytest.fixture
def use_model(client):
    """Serve a ScriptedModel replaying `script` to the AI endpoints; returns it so tests can inspect `seen`."""

    def _use(*script) -> ScriptedModel:
        model = ScriptedModel(script=list(script), seen=[])
        client._transport.app.dependency_overrides[chat_model] = lambda: model
        return model

    return _use
