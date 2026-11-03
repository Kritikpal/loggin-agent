# TODO: SECURITY — run llm-secure-patterns to apply LLM security patterns
"""The investigation agent's tools: search_logs (find events) and summarize_logs (count them, ADR-002). Both are
called in-process (no HTTP) with their collaborators injected, so tests can point them at the test database and a fake
embedder.

Every failure is returned to the model as text instead of raised: it can fix a bad argument and retry, and the graph
never crashes on a tool call. Unexpected errors are logged in full but reported to the model only in general terms.
"""

import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime

from langchain_core.tools import StructuredTool
from pydantic import Field, ValidationError

from app.agent.context import shorten_event
from app.core.database import SessionLocal
from app.core.embeddings import embed_query
from app.ingestion.coverage import coverage_for_all
from app.ingestion.sources import Source
from app.schemas.search_logs import (
    LogFilters, SearchLogsRequest, SearchLogsResponse, SourceCoverage, TimeAndLevelFilters,
)
from app.tools.search_logs import search_logs, searched_sources
from app.tools.search_queries import GroupBy, filters, group_counts

logger = logging.getLogger(__name__)

SEARCH_BUDGET = 3  # search_logs calls per question; the graph enforces it (app.agent.graph)
# Groq's free tier allows 8,000 tokens a minute, and every search result is sent again with each later model call.
# The model asked for top_k 10 on its own, which made three searches add up to 7,000+ tokens and hit HTTP 429.
DEFAULT_TOP_K = 5  # the API default is 10
MAX_TOP_K = 5  # hits shown for a search without a level filter, whatever top_k says
# A level filter is how the model lists every ERROR or WARN, so it may see more. Not 20: twenty 800-char stack
# traces plus the prompt made one request of 9,100 tokens, over Groq's 8,000 tokens a minute, and it can never run.
MAX_TOP_K_FILTERED = 10
FILTER_ONLY_QUERY = "error"  # stands in when the model searches by filter alone; the filters do the narrowing
MAX_RAW_TEXT = 800  # characters of one event's message shown to the model
# Characters of message in one search result, shared by its hits. Events are cut by lines (app.agent.context
# shorten_event): the message, then a stack trace's last "Caused by". 10 hits stay under ~1,600 tokens (ADR-003).
MAX_RESULT_TEXT = 4000
MIN_MESSAGE = 450  # never less: a WSO2 error line is 417 characters and its user id sits at character 334
MAX_CAUSE = 200  # the root cause line

# Sent with every model call (ADR-003 layer 2), so it holds only what the system prompt does not: what the tool shows
# and how to fill `query`. Strategy, time zones, relevance and coverage rules live in the system prompt (v2.0).
DESCRIPTION = """\
Find log events. Shows at most 5 hits, 10 with `levels`: a sample, so never count hits (summarize_logs counts). \
`query`: an identifier, plain words or both; no quotes or operators. "error" with `levels` lists by severity. \
"[...]" marks omitted stack frames. Scores rank hits within one search only."""


class AgentFilters(TimeAndLevelFilters):
    """The filters with short, agent-only descriptions: they go out with every model call. The model names files by
    their short id (no project prefix): a chat sees one project, so the prefix would only cost tokens (spec §6.2).
    The file rule sits here, where the argument is filled, on both tools (eval q5)."""

    files: list[str] | None = Field(default=None, description="Only files the user named; omit for all")
    start_time: datetime | None = Field(default=None, description="UTC ISO 8601, e.g. 2026-03-10T10:14:00Z")
    end_time: datetime | None = Field(default=None, description="UTC ISO 8601")
    levels: list[str] | None = Field(default=None, description="e.g. ERROR, WARN")


class AgentSearchArgs(AgentFilters):
    """The search arguments with a smaller default top_k, so the model gets (and pays for) fewer hits unless it asks."""

    # No min_length: the provider rejects an empty query before our code runs, and the model sends one when it
    # searches by level, time or file alone. run() swaps it for FILTER_ONLY_QUERY.
    query: str = Field(max_length=2000, description="Search text")
    # No le: the provider rejects a too-large top_k before our code runs (HTTP 400, the whole question fails).
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1)


class SummaryArgs(AgentFilters):
    group_by: GroupBy = "level"  # the enum already names the choices; a description would only cost tokens


def make_search_logs_tool(
    *,
    project_id: str,
    load_sources: Callable[[], tuple[Source, ...]],
    session_factory: Callable = SessionLocal,
    embed: Callable[[str], list[float]] = embed_query,
) -> StructuredTool:
    """`load_sources` must return only the project's files: that is what keeps a chat inside its project."""
    async def run(**arguments) -> str:
        requested = arguments.get("top_k", DEFAULT_TOP_K)
        cap = MAX_TOP_K_FILTERED if arguments.get("levels") else MAX_TOP_K
        query = arguments["query"].strip() or FILTER_ONLY_QUERY
        try:
            sources = load_sources()
            if problem := _unknown_file(arguments.get("files"), sources, project_id):
                return _error(problem)
            request = SearchLogsRequest(**_log_filters(arguments, project_id), query=query, top_k=min(requested, cap))
            async with session_factory() as session:
                response = await search_logs(request, session, sources=sources, embed=embed)
        except Exception:
            logger.exception("search_logs tool failed")
            return _error("search is temporarily unavailable")
        note = f"Showing the top {cap} of the {requested} requested results." if requested > cap else None
        return _compact(response, project_id, note)

    return StructuredTool.from_function(
        coroutine=run, name="search_logs", description=DESCRIPTION, args_schema=AgentSearchArgs,
        handle_validation_error=_validation_message,
    )


SUMMARY_BUDGET = 2  # summarize_logs calls per question, separate from the search budget (app.agent.graph)
MAX_GROUPS = 30  # rows shown, largest first; protects the token budget (ADR-003)
MAX_LEVEL_CHARS = 16  # the level is parsed from untrusted log text: keep it a label, never a sentence
MAX_COVERAGE_NAMES = 10  # file names listed in a coverage object; 30 daily files must not ride on every result

# When to use it is in the system prompt ("# summarize_logs"); this says only what it returns.
SUMMARY_DESCRIPTION = "Count events per level, day or file, with first and last times. No log text."


def make_summarize_logs_tool(
    *, project_id: str, load_sources: Callable[[], tuple[Source, ...]], session_factory: Callable = SessionLocal,
) -> StructuredTool:
    """`load_sources` must return only the project's files. Days are counted in the project's timezone."""
    async def run(**arguments) -> str:
        group_by = arguments.get("group_by", "level")
        try:
            sources = load_sources()
            if problem := _unknown_file(arguments.get("files"), sources, project_id):
                return _error(problem)
            request = LogFilters(**_log_filters(arguments, project_id))
            searched = searched_sources(request, sources)
            timezone = sources[0].timezone if sources else "UTC"  # one project, one timezone
            async with session_factory() as session:
                where = filters(request, [s.id for s in searched])
                rows = await group_counts(session, where, MAX_GROUPS + 1, group_by=group_by, timezone=timezone)
                coverage = await coverage_for_all(session, searched)
        except Exception:
            logger.exception("summarize_logs tool failed")
            return _error("summary is temporarily unavailable")
        groups = [_group(row, group_by, project_id) for row in rows[:MAX_GROUPS]]
        shown = {"groups": groups, "coverage": project_coverage(coverage, project_id)}
        if len(rows) > MAX_GROUPS:
            shown["note"] = f"Only the largest {MAX_GROUPS} groups are shown; narrow by time or level for the rest."
        return json.dumps(shown)

    return StructuredTool.from_function(
        coroutine=run, name="summarize_logs", description=SUMMARY_DESCRIPTION, args_schema=SummaryArgs,
        handle_validation_error=_validation_message,
    )


def project_coverage(coverage: Sequence[SourceCoverage], project_id: str) -> dict:
    """One coverage object for the project instead of one entry per file (spec §6.3). Only files that are not
    complete are named, at most MAX_COVERAGE_NAMES of them, grouped by status."""
    shown: dict = {"files": len(coverage), "indexed": sum(c.status != "not_indexed" for c in coverage)}
    more = 0
    for item in coverage:
        if item.status == "complete":
            continue
        if sum(len(v) for v in shown.values() if isinstance(v, list)) < MAX_COVERAGE_NAMES:
            shown.setdefault(item.status, []).append(_short(item.source_id, project_id))
        else:
            more += 1
    return {**shown, "more": more} if more else shown


def _group(row, group_by: str, project_id: str) -> dict:
    group = {
        "level": row.level[:MAX_LEVEL_CHARS] if row.level else None, "count": row.count,
        "first_at": row.first_at.isoformat() if row.first_at else None,
        "last_at": row.last_at.isoformat() if row.last_at else None,
    }
    if group_by == "day":
        return {"day": row.key, **group}
    if group_by == "file":
        return {"file": _short(row.key, project_id), **group}
    return group


def _log_filters(arguments: dict, project_id: str) -> dict:
    """The agent's arguments as LogFilters fields: short file names become the project's full source ids."""
    files = arguments.get("files")
    return {
        "source_ids": None if files is None else [f"{project_id}/{name}" for name in files],
        "start_time": arguments.get("start_time"), "end_time": arguments.get("end_time"),
        "levels": arguments.get("levels"),
    }


def _unknown_file(files: list[str] | None, sources: tuple[Source, ...], project_id: str) -> str | None:
    """Checked before the search, so the model only ever sees this project's short names, never a full id."""
    known = [_short(source.id, project_id) for source in sources]
    for name in files or ():
        if name not in known:
            return f"Unknown file '{name}'. Files in this project: {known}"
    return None


def _short(source_id: str, project_id: str) -> str:
    return source_id.removeprefix(f"{project_id}/")


def _compact(response: SearchLogsResponse, project_id: str, note: str | None = None) -> str:
    """The response as the model needs it: what, where and when, without ids, byte offsets or file paths. A stack
    trace can be 20,000 characters, and the model is billed (and rate limited) for every one of them."""
    # 5 hits get 800 characters of message each, 10 hits 450; plus each trace's root cause
    message_max = max(MIN_MESSAGE, min(MAX_RAW_TEXT, MAX_RESULT_TEXT // max(len(response.results), 1)))
    hits = [
        {
            "file": _short(hit.source_id, project_id),
            "occurred_at": hit.occurred_at.isoformat() if hit.occurred_at else None,
            "level": hit.level,
            "line_start": hit.locator.get("line_start"),
            "line_end": hit.locator.get("line_end"),
            "raw_text": shorten_event(hit.raw_text, first_max=message_max, cause_max=MAX_CAUSE),
            "score": round(hit.score, 4),
            "matched_by": hit.matched_by,
        }
        for hit in response.results
    ]
    shown = {"results": hits, "coverage": project_coverage(response.coverage, project_id)}
    return json.dumps({**shown, "note": note} if note else shown)


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _validation_message(exc: ValidationError) -> str:
    problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
    return _error(f"invalid search arguments: {problems}")
