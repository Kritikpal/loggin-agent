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
