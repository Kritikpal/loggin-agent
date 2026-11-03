"""The agent's search_logs tool: arguments, results, and that every failure comes back to the model as text."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest

from app.agent.tools import make_search_logs_tool
from app.ingestion.sources import FileSource
from app.schemas.search_logs import SourceCoverage
from tests.search_helpers import (
    DEV_AUTH_ID, DEV_AUTH_LOG, copy_fixture, dev_auth_source, fake_embed_query, index_file, insert_rows, make_row,
)


def make_tool(session_factory, sources=(), project_id="demo"):
    return make_search_logs_tool(
        project_id=project_id, session_factory=session_factory, load_sources=lambda: tuple(sources),
        embed=fake_embed_query)


def bare_tool():
    """A tool for schema checks: no database is touched."""
    return make_search_logs_tool(project_id="demo", load_sources=lambda: ())


async def call(tool, **arguments) -> dict:
    return json.loads(await tool.ainvoke(arguments))


def test_the_tool_takes_the_search_request_fields_and_nothing_else():
    tool = bare_tool()

    assert tool.name == "search_logs"
    assert set(tool.args) == {"query", "top_k", "start_time", "end_time", "levels"}


async def test_a_failing_database_is_reported_without_internals():
    def broken_session_factory():
        raise RuntimeError("password=hunter2 host=db.internal")

    result = await call(make_tool(broken_session_factory), query="anything")

    assert result == {"error": "search is temporarily unavailable"}


def test_the_schema_the_model_provider_checks_allows_an_empty_query():
    # Groq validates tool arguments against this schema before our code runs; a minLength would turn
    # {"query": "", "levels": ["ERROR"]} into a failed request instead of a search.
    assert "minLength" not in bare_tool().args["query"]


async def many_error_events(session_factory, tmp_path, count: int):
    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    await insert_rows(session_factory, DEV_AUTH_ID, [
        make_row(n, f"pool exhausted event {n}", level="ERROR", occurred_at=datetime(2026, 3, 10, tzinfo=UTC))
        for n in range(1, count + 1)])
    return make_tool(session_factory, [source])


def test_a_top_k_over_the_api_limit_is_clamped_not_rejected():
    # The provider checks tool arguments against the schema before our code runs: a "maximum" there turned a
    # model's top_k 100 into a failed request (HTTP 400) instead of a search.
    def broken_session_factory():
        raise RuntimeError("no database in this test")

    assert "maximum" not in bare_tool().args["top_k"]
    result = asyncio.run(call(make_tool(broken_session_factory), query="UserNotFound", top_k=100))

    assert result == {"error": "search is temporarily unavailable"}  # it got past validation to the search


def test_the_tool_description_says_hits_are_never_a_count():
    from app.agent.tools import DESCRIPTION

    assert "never count" in DESCRIPTION and "summarize_logs" in DESCRIPTION


def test_neither_tool_can_narrow_to_a_file_so_every_search_covers_the_whole_project():
    # spec §6.5: the golden set scored better and used fewer calls without a file filter (2026-11-03 eval)
    from app.agent.tools import make_summarize_logs_tool

    for tool in (bare_tool(), make_summarize_logs_tool(project_id="demo", load_sources=lambda: ())):
        assert "files" not in tool.args and "source_ids" not in tool.args


async def test_a_reversed_time_range_is_explained_not_raised():
    tool = bare_tool()

    answer = await tool.ainvoke({"query": "error", "start_time": "2026-03-10T11:00:00Z",
                                 "end_time": "2026-03-10T10:00:00Z"})

    assert "start_time must not be after end_time" in answer


def hits_response(count: int, chars: int):
    from app.schemas.search_logs import SearchHit, SearchLogsResponse

    hit = dict(source_id="p/s", occurred_at=None, level="ERROR", locator={"line_start": 1, "line_end": 2},
               score=0.5, matched_by=["keyword"])
    trace = "\n\tat a.b(C.java:1)" * 100  # a stack trace: message line, frames, root cause
    results = [SearchHit(event_id=uuid.uuid4(), raw_text=f"HEAD{n} " + "x" * chars + trace + f"\nCaused by: TAIL{n}",
                         **hit) for n in range(count)]
    return SearchLogsResponse(results=results, coverage=[])


def test_many_long_hits_share_one_text_budget_so_a_batched_turn_stays_under_the_token_limit():
    # ADR-003: three batched searches of 10 stack traces at 800 chars each made one request of ~11,000 tokens
    from app.agent.tools import MAX_CAUSE, MIN_MESSAGE, _compact

    hits = json.loads(_compact(hits_response(10, 3000), "p"))["results"]

    shown = sum(len(hit["raw_text"]) for hit in hits)
    assert shown <= 10 * (MIN_MESSAGE + MAX_CAUSE + 20)  # ~6,700 chars, ~1,700 tokens: far below Groq's 8,000
    assert all(hit["raw_text"].startswith(f"HEAD{n}") and hit["raw_text"].endswith(f"Caused by: TAIL{n}")
               for n, hit in enumerate(hits))


def test_a_few_hits_keep_a_longer_message():
    from app.agent.context import OMITTED
    from app.agent.tools import MAX_RAW_TEXT, _compact

    hits = json.loads(_compact(hits_response(5, 3000), "p"))["results"]

    assert all(MAX_RAW_TEXT <= len(hit["raw_text"].split(OMITTED)[0]) <= MAX_RAW_TEXT + 10 for hit in hits)


def test_both_tool_definitions_stay_small_because_every_model_call_resends_them():
    # ADR-003 layer 2: 3,980 chars before the trim; strategy belongs in the system prompt, not here
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from app.agent.tools import make_summarize_logs_tool

    summary = make_summarize_logs_tool(project_id="demo", load_sources=lambda: ())
    sent = sum(len(json.dumps(convert_to_openai_tool(t))) for t in (bare_tool(), summary))

    assert sent <= 2100  # 2,498 after the first trim, 2,018 with prompt v2.0


# ---- one project per chat (spec 2026-11-03 §6) ----

def test_hits_use_short_file_ids():
    from app.agent.tools import _compact

    (hit,) = json.loads(_compact(hits_response(1, 10), "p"))["results"]

    assert hit["file"] == "s" and "source_id" not in hit


def test_coverage_is_one_object_for_the_project():
    from app.agent.tools import project_coverage

    coverage = [SourceCoverage(source_id=f"p/f{n:02}", status="not_indexed", indexed_through=None) for n in range(12)]
    coverage.append(SourceCoverage(source_id="p/done", status="complete", indexed_through=None))

    shown = project_coverage(coverage, "p")

    assert shown == {"files": 13, "indexed": 1, "not_indexed": [f"f{n:02}" for n in range(10)], "more": 2}


def test_empty_project_coverage():
    from app.agent.tools import project_coverage

    assert project_coverage([], "p") == {"files": 0, "indexed": 0}


@pytest.mark.integration
async def test_search_tool_never_sees_other_projects(tmp_path, session_factory):
    at = datetime(2026, 10, 30, tzinfo=UTC)
    await insert_rows(session_factory, "a/x", [make_row(1, "pool exhausted in a", level="ERROR", occurred_at=at)])
    await insert_rows(session_factory, "b/other", [make_row(1, "pool exhausted in b", level="ERROR", occurred_at=at)])
    tool = make_tool(session_factory, [FileSource(id="a/x", path=tmp_path / "x.log")], project_id="a")

    found = await call(tool, query="pool exhausted")
    narrowed = await call(tool, query="pool exhausted", source_ids=["b/other"])

    assert {hit["file"] for hit in found["results"]} == {"x"}
    assert "error" in narrowed and "source_ids" in narrowed["error"]  # no way to name another project's file
