"""Per-file stats and missing files for the projects API (spec 2026-11-03 §5)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingestion.coverage import FileStats, file_stats, orphan_ids
from app.ingestion.projects import Project
from app.ingestion.sources import FileSource
from app.models.log_event import SourceIndexState
from tests.search_helpers import insert_rows, make_row

pytestmark = pytest.mark.integration

AT = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)


def project(project_id: str, *stems: str) -> Project:
    files = tuple(FileSource(id=f"{project_id}/{s}", path=Path(f"{s}.log")) for s in stems)
    return Project(id=project_id, title=project_id, folder=Path(project_id), files=files)


async def add_states(session_factory, *source_ids: str) -> None:
    async with session_factory() as session:
        session.add_all(SourceIndexState(source_id=s) for s in source_ids)
        await session.commit()


async def test_file_stats_counts_and_parsed_ratio(session_factory):
    await insert_rows(session_factory, "p/a", [
        make_row(1, "one", level="ERROR", occurred_at=AT),
        make_row(2, "two", level="INFO", occurred_at=AT),
        make_row(3, "three", level="WARN", occurred_at=AT.replace(hour=11)),
        make_row(4, "no level or time", level=None, occurred_at=None),
    ])

    async with session_factory() as session:
        stats = await file_stats(session, ["p/a"])

    assert stats == {"p/a": FileStats(event_count=4, first_at=AT, last_at=AT.replace(hour=11), parsed_ratio=0.75)}


async def test_file_stats_leaves_out_ids_without_events(session_factory):
    await insert_rows(session_factory, "p/a", [make_row(1, "x", level="INFO", occurred_at=AT)])

    async with session_factory() as session:
        assert set(await file_stats(session, ["p/a", "p/empty", "q/a"])) == {"p/a"}


async def test_orphan_ids_are_state_rows_with_no_file(session_factory):
    await add_states(session_factory, "p/a", "p/gone", "p/also-gone", "q/other")

    async with session_factory() as session:
        assert await orphan_ids(session, project("p", "a")) == ["p/also-gone", "p/gone"]


async def test_orphan_ids_do_not_match_like_wildcards(session_factory):
    await add_states(session_factory, "wso2xgw/a", "ab/x", "a_b/x")

    async with session_factory() as session:
        assert await orphan_ids(session, project("wso2_gw")) == []
        assert await orphan_ids(session, project("a")) == []
        assert await orphan_ids(session, project("a_b")) == ["a_b/x"]
