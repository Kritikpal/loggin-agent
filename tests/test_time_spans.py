"""time_spans: the first and last event time of each source, so the agent can place a date-less time in the logs."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingestion.coverage import time_spans
from tests.search_helpers import dev_auth_source, insert_rows, make_row

pytestmark = pytest.mark.integration


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 3, 10, hour, minute, tzinfo=UTC)


async def spans_for(session_factory, *source_ids: str):
    sources = tuple(dev_auth_source(Path("x.log"), source_id) for source_id in source_ids)
    async with session_factory() as session:
        return await time_spans(session, sources)


async def test_each_source_gets_its_first_and_last_event_time(session_factory):
    await insert_rows(session_factory, "a", [
        make_row(1, "late", level="INFO", occurred_at=at(11, 0)),
        make_row(2, "early", level="INFO", occurred_at=at(10, 0)),
    ])
    await insert_rows(session_factory, "b", [make_row(1, "only", level="INFO", occurred_at=at(9, 30))])

    assert await spans_for(session_factory, "a", "b") == {"a": (at(10, 0), at(11, 0)), "b": (at(9, 30), at(9, 30))}


async def test_a_source_without_timestamped_events_is_left_out(session_factory):
    await insert_rows(session_factory, "a", [make_row(1, "no time", level="INFO", occurred_at=None)])

    assert await spans_for(session_factory, "a", "never-indexed") == {}


async def test_only_the_asked_for_sources_are_returned(session_factory):
    await insert_rows(session_factory, "a", [make_row(1, "x", level="INFO", occurred_at=at(10, 0))])
    await insert_rows(session_factory, "b", [make_row(1, "y", level="INFO", occurred_at=at(11, 0))])

    assert set(await spans_for(session_factory, "a")) == {"a"}
