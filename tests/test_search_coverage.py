"""Index coverage (AC7): coverage_for, the coverage in every search response, freshness, and GET /api/v1/sources.
mtimes are pinned with os.utime relative to the state row's updated_at, so no test depends on filesystem timing."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.ingestion.readers.base import Cursor
from app.ingestion.sources import FileSource
from app.ingestion.store import record_error
from tests.search_helpers import (
    DEV_AUTH_ID, DEV_AUTH_LOG, HOLD_SECONDS, OLD_MTIME, SPRING_ID, SPRING_LOG, api_client, copy_fixture, coverage,
    dev_auth_source, hit_lines, index_file, search, set_mtime, spring_source, state_row,
)

pytestmark = pytest.mark.integration

LAST_EVENT_AT = datetime(2026, 3, 10, 10, 14, 8, 219000, tzinfo=UTC)  # line 167, 15:44:08.219 Asia/Kolkata
NEW_EVENTS = (
    "2026-03-10 15:50:01.000 [main] ERROR c.t.smrd.Probe - freshness probe one\n"
    "2026-03-10 15:50:02.000 [main] WARN  c.t.smrd.Probe - freshness probe two\n"
    "2026-03-10 15:50:03.000 [main] INFO  c.t.smrd.Probe - freshness probe three\n"
)
HOUR = timedelta(hours=1)


@pytest.fixture
def source(tmp_path) -> FileSource:
    return dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))


async def updated_at(session_factory, source_id: str = DEV_AUTH_ID) -> datetime:
    return (await state_row(session_factory, source_id)).updated_at


def append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(text)


async def test_the_sources_endpoint_with_no_sources_is_an_empty_list(session_factory):
    async with api_client(session_factory, []) as http:
        res = await http.get("/api/v1/sources")

    assert (res.status_code, res.json()) == (200, [])
