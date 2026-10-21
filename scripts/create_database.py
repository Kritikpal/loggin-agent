"""Create the application database if it is missing. Never drops or alters an existing database.

    python -m scripts.create_database           # DATABASE_URL's database
    python -m scripts.create_database --test    # the isolated test database
"""

import argparse
import asyncio
import re

import asyncpg
from sqlalchemy.engine import make_url

from app.core.config import get_settings

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


async def ensure_database(url: str) -> bool:
    """Return True if the database was created, False if it already existed."""
    target = make_url(url)
    name = target.database or ""
    if not IDENTIFIER.fullmatch(name):  # identifiers can't be bound parameters, so validate strictly
        raise ValueError(f"Unsafe database name: {name!r}")
    conn = await asyncpg.connect(
        host=target.host, port=target.port, user=target.username, password=target.password, database="postgres"
    )
    try:
        if await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            return False
        await conn.execute(f'CREATE DATABASE "{name}"')
        return True
    finally:
        await conn.close()


async def main(test: bool) -> None:
    settings = get_settings()
    url = settings.resolved_test_database_url if test else settings.database_url
    name = make_url(url).database
    created = await ensure_database(url)
    print(f"Database '{name}' {'created' if created else 'already exists - left untouched'}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", action="store_true", help="create the test database instead")
    asyncio.run(main(parser.parse_args().test))
