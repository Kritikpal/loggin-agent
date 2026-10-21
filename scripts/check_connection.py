"""Verify the PostgreSQL connection from DATABASE_URL:  python -m scripts.check_connection"""

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings


async def main() -> int:
    url = get_settings().database_url
    shown = make_url(url).render_as_string(hide_password=True)
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            version = await conn.scalar(text("SELECT version()"))
            tables = (
                await conn.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"))
            ).all()
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
        print(f"FAILED to connect to {shown}\n  {type(exc).__name__}: {exc}")
        print("  If the database is missing, run: python -m scripts.create_database")
        return 1
    finally:
        await engine.dispose()
    print(f"Connected to {shown}\n  {version}\n  public tables: {', '.join(tables) or '(none - run migrations)'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
