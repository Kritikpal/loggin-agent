"""The agent's summarize_logs tool: counts per source and level, never log text, failures returned as text."""

import json
from datetime import UTC, datetime

import pytest

from app.agent.tools import make_summarize_logs_tool
from tests.search_helpers import (
    DEV_AUTH_ID, DEV_AUTH_LOG, copy_fixture, dev_auth_source, index_file, insert_rows, make_row,
)


def make_tool(session_factory, sources=()):
    return make_summarize_logs_tool(session_factory=session_factory, load_sources=lambda: tuple(sources))


async def call(tool, **arguments) -> dict:
    return json.loads(await tool.ainvoke(arguments))


def by_level(result: dict) -> dict[str, dict]:
    return {group["level"]: group for group in result["groups"]}


def test_the_tool_takes_only_filters():
    tool = make_summarize_logs_tool()

    assert tool.name == "summarize_logs"
    assert set(tool.args) == {"source_ids", "levels", "start_time", "end_time"}


async def test_a_failing_database_is_reported_without_internals():
    def broken_session_factory():
        raise RuntimeError("password=hunter2 host=db.internal")

    result = await call(make_tool(broken_session_factory))

    assert result == {"error": "summary is temporarily unavailable"}


@pytest.mark.integration
async def test_counts_each_level_with_first_and_last_time_and_no_log_text(session_factory, tmp_path):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    await index_file(source, session_factory)

    result = await call(make_tool(session_factory, [source]))

    levels = by_level(result)
    assert levels["ERROR"]["count"] == 3
    assert levels["WARN"]["count"] == 5
    assert levels["WARN"]["first_at"] <= levels["WARN"]["last_at"]
    assert set(levels["ERROR"]) == {"source_id", "level", "count", "first_at", "last_at"}
    assert [(c["source_id"], c["status"]) for c in result["coverage"]] == [(DEV_AUTH_ID, "complete")]


@pytest.mark.integration
async def test_level_and_time_filters_narrow_the_counts(session_factory, tmp_path):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    early, late = datetime(2026, 3, 10, 10, 0, tzinfo=UTC), datetime(2026, 3, 10, 11, 0, tzinfo=UTC)
    await insert_rows(session_factory, DEV_AUTH_ID, [
        make_row(1, "a", level="ERROR", occurred_at=early),
        make_row(2, "b", level="ERROR", occurred_at=late),
        make_row(3, "c", level="INFO", occurred_at=late),
    ])
    tool = make_tool(session_factory, [source])

    errors = await call(tool, levels=["error"])
    late_only = await call(tool, start_time="2026-03-10T10:30:00Z")

    assert [(g["level"], g["count"]) for g in errors["groups"]] == [("ERROR", 2)]
    assert sorted((g["level"], g["count"]) for g in late_only["groups"]) == [("ERROR", 1), ("INFO", 1)]


@pytest.mark.integration
async def test_an_unknown_source_is_refused_with_the_allowed_ids(session_factory, tmp_path):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))

    result = await call(make_tool(session_factory, [source]), source_ids=["payments"])

    assert "error" in result and DEV_AUTH_ID in result["error"]
