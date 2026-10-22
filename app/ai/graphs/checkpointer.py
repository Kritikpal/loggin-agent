"""LangGraph's PostgreSQL checkpointer on the application's own database. Its tables (checkpoints, checkpoint_blobs,
checkpoint_writes, checkpoint_migrations) are created by `setup()`, not by Alembic."""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.engine import make_url

POOL_SIZE = 5
CONNECT_TIMEOUT = 10  # seconds


@asynccontextmanager
async def open_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """`database_url` is the app's SQLAlchemy URL (postgresql+asyncpg://...); psycopg gets the plain equivalent."""
    if sys.platform == "win32" and isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop):
        # psycopg would otherwise hang for 30s and time out; say what to do instead.
        raise RuntimeError(
            "The PostgreSQL checkpointer needs the SelectorEventLoop on Windows. "
            "Start uvicorn with: --loop asyncio:SelectorEventLoop"
        )
    dsn = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    pool = AsyncConnectionPool(
        dsn,
        min_size=1,
        max_size=POOL_SIZE,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},  # required by the saver
    )
    try:
        await pool.open(wait=True, timeout=CONNECT_TIMEOUT)
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        yield saver
    finally:
        await pool.close()
