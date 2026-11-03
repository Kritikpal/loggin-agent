"""The agent's summarize_logs tool: counts per source and level, never log text, failures returned as text."""

import json
from datetime import UTC, datetime

import pytest

from app.agent.tools import make_summarize_logs_tool
from app.ingestion.sources import FileSource
from tests.search_helpers import (
    DEV_AUTH_ID, DEV_AUTH_LOG, copy_fixture, dev_auth_source, index_file, insert_rows, make_row,
)


def make_tool(session_factory, sources=(), project_id="demo"):
    return make_summarize_logs_tool(
        project_id=project_id, session_factory=session_factory, load_sources=lambda: tuple(sources))


async def call(tool, **arguments) -> dict:
    return json.loads(await tool.ainvoke(arguments))


def by_level(result: dict) -> dict[str, dict]:
    return {group["level"]: group for group in result["groups"]}


def test_the_tool_takes_only_filters():
    tool = make_summarize_logs_tool(project_id="demo", load_sources=lambda: ())

    assert tool.name == "summarize_logs"
    assert set(tool.args) == {"levels", "start_time", "end_time", "group_by"}


async def test_a_failing_database_is_reported_without_internals():
    def broken_session_factory():
        raise RuntimeError("password=hunter2 host=db.internal")

    result = await call(make_tool(broken_session_factory))

    assert result == {"error": "summary is temporarily unavailable"}


def sources(tmp_path, *stems: str, timezone: str = "UTC") -> list[FileSource]:
    return [FileSource(id=f"p/{s}", path=tmp_path / f"{s}.log", timezone=timezone) for s in stems]


@pytest.mark.integration
async def test_summary_group_by_level_counts_the_whole_project(tmp_path, session_factory):
    at = datetime(2026, 10, 30, tzinfo=UTC)
    await insert_rows(session_factory, "p/a", [make_row(n, f"e{n}", level="ERROR", occurred_at=at) for n in (1, 2)])
    await insert_rows(session_factory, "p/b", [make_row(1, "e", level="ERROR", occurred_at=at),
                                               make_row(2, "i", level="INFO", occurred_at=at)])

    result = await call(make_tool(session_factory, sources(tmp_path, "a", "b"), "p"))

    assert [(g["level"], g["count"]) for g in result["groups"]] == [("ERROR", 3), ("INFO", 1)]
    assert "file" not in result["groups"][0] and "day" not in result["groups"][0]
    assert result["coverage"]["files"] == 2


@pytest.mark.integration
async def test_summary_group_by_day_uses_project_timezone(tmp_path, session_factory):
    await insert_rows(session_factory, "p/a", [
        make_row(1, "late evening UTC", level="ERROR", occurred_at=datetime(2026, 10, 29, 20, 0, tzinfo=UTC)),
        make_row(2, "morning UTC", level="ERROR", occurred_at=datetime(2026, 10, 29, 8, 0, tzinfo=UTC)),
    ])
    tool = make_tool(session_factory, sources(tmp_path, "a", timezone="Asia/Kolkata"), "p")

    result = await call(tool, group_by="day")

    assert sorted((g["day"], g["count"]) for g in result["groups"]) == [("2026-10-29", 1), ("2026-10-30", 1)]


@pytest.mark.integration
async def test_summary_rows_are_ordered_by_count_and_the_smallest_are_cut(tmp_path, session_factory):
    stems = [f"f{n:02}" for n in range(1, 32)]
    at = datetime(2026, 10, 30, tzinfo=UTC)
    for n, stem in enumerate(stems, 1):
        await insert_rows(session_factory, f"p/{stem}",
                          [make_row(i, f"e{i}", level="ERROR", occurred_at=at) for i in range(1, n + 1)])

    result = await call(make_tool(session_factory, sources(tmp_path, *stems), "p"), group_by="file")

    assert [g["file"] for g in result["groups"]] == list(reversed(stems))[:30]
    assert "narrow by time or level" in result["note"]
