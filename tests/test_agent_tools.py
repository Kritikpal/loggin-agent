"""The agent's search_logs tool: arguments, results, and that every failure comes back to the model as text."""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.agent.tools import make_search_logs_tool
from app.schemas.search_logs import SearchLogsRequest
from tests.search_helpers import (
    DEV_AUTH_ID, DEV_AUTH_LOG, copy_fixture, dev_auth_source, fake_embed_query, index_file, insert_rows, make_row,
)


def make_tool(session_factory, sources=()):
    return make_search_logs_tool(
        session_factory=session_factory, load_sources=lambda: tuple(sources), embed=fake_embed_query)


async def call(tool, **arguments) -> dict:
    return json.loads(await tool.ainvoke(arguments))


def test_the_tool_takes_the_search_request_fields_and_nothing_else():
    tool = make_search_logs_tool()

    assert tool.name == "search_logs"
    assert set(tool.args) == set(SearchLogsRequest.model_fields)


async def test_a_failing_database_is_reported_without_internals():
    def broken_session_factory():
        raise RuntimeError("password=hunter2 host=db.internal")

    result = await call(make_tool(broken_session_factory), query="anything")

    assert result == {"error": "search is temporarily unavailable"}


@pytest.mark.integration
async def test_a_search_returns_hits_with_lines_and_coverage(session_factory, tmp_path):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    await index_file(source, session_factory)

    result = await call(make_tool(session_factory, [source]), query="SPRING_DATASOURCE_URL", top_k=5)

    assert {hit["line_start"] for hit in result["results"]} >= {18, 158, 167}
    assert [(c["source_id"], c["status"]) for c in result["coverage"]] == [(DEV_AUTH_ID, "complete")]


@pytest.mark.integration
async def test_a_huge_event_is_shortened_to_its_head_and_tail_and_ids_are_dropped(session_factory, tmp_path):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    trace = "HEADMARK java.lang.Error\n" + "\tat frame\n" * 5000 + "Caused by: TAILMARK"
    await insert_rows(session_factory, DEV_AUTH_ID, [
        make_row(7, "datasource failure", level="ERROR", occurred_at=datetime(2026, 3, 10, tzinfo=UTC), raw_text=trace)])

    result = await call(make_tool(session_factory, [source]), query="datasource failure")

    (hit,) = result["results"]
    assert len(hit["raw_text"]) < 1000 < len(trace)
    assert "HEADMARK" in hit["raw_text"] and "Caused by: TAILMARK" in hit["raw_text"]
    assert "chars omitted" in hit["raw_text"]
    assert set(hit) == {"source_id", "occurred_at", "level", "line_start", "line_end", "raw_text", "score", "matched_by"}


def test_the_schema_the_model_provider_checks_allows_an_empty_query():
    # Groq validates tool arguments against this schema before our code runs; a minLength would turn
    # {"query": "", "levels": ["ERROR"]} into a failed request instead of a search.
    assert "minLength" not in make_search_logs_tool().args["query"]


@pytest.mark.integration
async def test_a_filter_only_search_with_an_empty_query_still_searches(session_factory, tmp_path):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    await index_file(source, session_factory)

    result = await call(make_tool(session_factory, [source]), query="", levels=["ERROR"], top_k=50)

    assert sorted(hit["line_start"] for hit in result["results"]) == [158, 159, 167]


async def many_error_events(session_factory, tmp_path, count: int):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    await insert_rows(session_factory, DEV_AUTH_ID, [
        make_row(n, f"pool exhausted event {n}", level="ERROR", occurred_at=datetime(2026, 3, 10, tzinfo=UTC))
        for n in range(1, count + 1)])
    return make_tool(session_factory, [source])


@pytest.mark.integration
async def test_an_unfiltered_search_shows_at_most_five_hits_however_many_are_asked_for(session_factory, tmp_path):
    tool = await many_error_events(session_factory, tmp_path, 8)

    default = await call(tool, query="pool exhausted")
    asked_for_ten = await call(tool, query="pool exhausted", top_k=10)

    assert len(default["results"]) == 5 and "note" not in default
    assert len(asked_for_ten["results"]) == 5
    assert "top 5 of the 10 requested" in asked_for_ten["note"]


@pytest.mark.integration
async def test_a_level_filter_allows_enumerating_up_to_ten_hits(session_factory, tmp_path):
    tool = await many_error_events(session_factory, tmp_path, 25)

    eight = await call(tool, query="pool exhausted", levels=["ERROR"], top_k=8)
    fifty = await call(tool, query="pool exhausted", levels=["ERROR"], top_k=50)

    assert len(eight["results"]) == 8 and "note" not in eight
    assert len(fifty["results"]) == 10 and "top 10 of the 50 requested" in fifty["note"]


def test_a_top_k_over_the_api_limit_is_clamped_not_rejected():
    # The provider checks tool arguments against the schema before our code runs: a "maximum" there turned a
    # model's top_k 100 into a failed request (HTTP 400) instead of a search.
    def broken_session_factory():
        raise RuntimeError("no database in this test")

    assert "maximum" not in make_search_logs_tool().args["top_k"]
    result = asyncio.run(call(make_tool(broken_session_factory), query="UserNotFound", top_k=100))

    assert result == {"error": "search is temporarily unavailable"}  # it got past validation to the search


def test_the_tool_description_says_hits_are_never_a_count():
    from app.agent.tools import DESCRIPTION

    assert "never count" in DESCRIPTION and "summarize_logs" in DESCRIPTION


def test_the_tool_description_says_to_filter_by_source_only_when_the_user_named_one():
    from app.agent.tools import DESCRIPTION

    assert "only when the user named" in DESCRIPTION


@pytest.mark.integration
async def test_an_unknown_source_lists_the_allowed_ones(session_factory, tmp_path):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))

    result = await call(make_tool(session_factory, [source]), query="error", source_ids=["nope"])

    assert "nope" in result["error"]
    assert DEV_AUTH_ID in result["error"]


async def test_a_reversed_time_range_is_explained_not_raised():
    tool = make_search_logs_tool()

    answer = await tool.ainvoke({"query": "error", "start_time": "2026-03-10T11:00:00Z",
                                 "end_time": "2026-03-10T10:00:00Z"})

    assert "start_time must not be after end_time" in answer
