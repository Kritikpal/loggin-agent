# TODO: SECURITY — run llm-secure-patterns to apply LLM security patterns
"""The investigation agent's tools: search_logs (find events) and summarize_logs (count them, ADR-002). Both are
called in-process (no HTTP) with their collaborators injected, so tests can point them at the test database and a fake
embedder.

Every failure is returned to the model as text instead of raised: it can fix a bad argument and retry, and the graph
never crashes on a tool call. Unexpected errors are logged in full but reported to the model only in general terms.
"""

import json
import logging
from collections.abc import Callable

from langchain_core.tools import StructuredTool
from pydantic import Field, ValidationError

from app.agent.context import shorten_event
from app.controllers.dependencies import get_sources
from app.core.database import SessionLocal
from app.core.embeddings import embed_query
from app.core.errors import NotFoundError
from app.ingestion.sources import Source
from app.ingestion.coverage import coverage_for_all
from app.schemas.search_logs import LevelCount, LogFilters, SearchLogsRequest, SearchLogsResponse
from app.tools.search_logs import search_logs, searched_sources
from app.tools.search_queries import filters, level_counts

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

# Sent with every model call (ADR-003 layer 2), so it holds only how to fill the arguments; the search strategy, time
# zones, relevance and coverage rules live in the system prompt.
DESCRIPTION = """\
Search the indexed logs (read-only). Returns events (source_id, line, time, level, raw_text, matched_by) and `coverage`.
Long events keep their message and last "Caused by"; frames become "[...]". At most 5 hits, 10 with `levels`: a
sample, so never count hits for "how many" (summarize_logs counts). `query`: an identifier alone, plain words, or both; no quotes or operators;
"error" with `levels` lists by severity. `source_ids` only when the user named the source. Scores are rank-based: never
compare them across searches."""


class AgentSearchArgs(SearchLogsRequest):
    """The API's request with a smaller default top_k, so the model gets (and pays for) fewer hits unless it asks."""

    # No min_length: the provider rejects an empty query before our code runs, and the model sends one when it
    # searches by level, time or source alone. run() swaps it for FILTER_ONLY_QUERY.
    query: str = Field(max_length=2000, description="Search text")
    # No le: the provider rejects a too-large top_k before our code runs (HTTP 400, the whole question fails).
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, description=SearchLogsRequest.model_fields["top_k"].description)


def make_search_logs_tool(
    *,
    session_factory: Callable = SessionLocal,
    load_sources: Callable[[], tuple[Source, ...]] = get_sources,
    embed: Callable[[str], list[float]] = embed_query,
) -> StructuredTool:
    async def run(**arguments) -> str:
        requested = arguments.get("top_k", DEFAULT_TOP_K)
        cap = MAX_TOP_K_FILTERED if arguments.get("levels") else MAX_TOP_K
        request = SearchLogsRequest(**{
            **arguments, "query": arguments["query"].strip() or FILTER_ONLY_QUERY, "top_k": min(requested, cap)})
        try:
            sources = load_sources()
            async with session_factory() as session:
                response = await search_logs(request, session, sources=sources, embed=embed)
        except NotFoundError as exc:
            return _error(f"{exc.message}. Allowed source_ids: {[source.id for source in sources]}")
        except Exception:
            logger.exception("search_logs tool failed")
            return _error("search is temporarily unavailable")
        note = f"Showing the top {cap} of the {requested} requested results." if requested > cap else None
        return _compact(response, note)

    return StructuredTool.from_function(
        coroutine=run, name="search_logs", description=DESCRIPTION, args_schema=AgentSearchArgs,
        handle_validation_error=_validation_message,
    )


SUMMARY_BUDGET = 2  # summarize_logs calls per question, separate from the search budget (app.agent.graph)
MAX_GROUPS = 30  # source/level rows shown; more only happens when a source writes many odd level spellings
MAX_LEVEL_CHARS = 16  # the level is parsed from untrusted log text: keep it a label, never a sentence

# When to use it is in the system prompt ("Overview first"); this says only what it returns.
SUMMARY_DESCRIPTION = """\
Count indexed events per source and level, with first and last times, plus `coverage`. Read-only, no log text, no
search used. Same filters as search_logs."""


def make_summarize_logs_tool(
    *, session_factory: Callable = SessionLocal, load_sources: Callable[[], tuple[Source, ...]] = get_sources,
) -> StructuredTool:
    async def run(**arguments) -> str:
        request = LogFilters(**arguments)
        sources: tuple[Source, ...] = ()
        try:
            sources = load_sources()
            searched = searched_sources(request, sources)
            async with session_factory() as session:
                rows = await level_counts(session, filters(request, [s.id for s in searched]), MAX_GROUPS + 1)
                coverage = await coverage_for_all(session, searched)
        except NotFoundError as exc:
            return _error(f"{exc.message}. Allowed source_ids: {[source.id for source in sources]}")
        except Exception:
            logger.exception("summarize_logs tool failed")
            return _error("summary is temporarily unavailable")
        groups = [_group(row) for row in rows[:MAX_GROUPS]]
        shown = {"groups": groups, "coverage": [c.model_dump(mode="json") for c in coverage]}
        if len(rows) > MAX_GROUPS:
            shown["note"] = f"Only the largest {MAX_GROUPS} groups are shown; narrow by source or level for the rest."
        return json.dumps(shown)

    return StructuredTool.from_function(
        coroutine=run, name="summarize_logs", description=SUMMARY_DESCRIPTION, args_schema=LogFilters,
        handle_validation_error=_validation_message,
    )


def _group(row) -> dict:
    level = row.level[:MAX_LEVEL_CHARS] if row.level else None
    group = LevelCount(source_id=row.source_id, level=level, count=row.count, first_at=row.first_at, last_at=row.last_at)
    return group.model_dump(mode="json")


def _compact(response: SearchLogsResponse, note: str | None = None) -> str:
    """The response as the model needs it: what, where and when, without ids, byte offsets or file paths. A stack
    trace can be 20,000 characters, and the model is billed (and rate limited) for every one of them."""
    # 5 hits get 800 characters of message each, 10 hits 450; plus each trace's root cause
    message_max = max(MIN_MESSAGE, min(MAX_RAW_TEXT, MAX_RESULT_TEXT // max(len(response.results), 1)))
    hits = [
        {
            "source_id": hit.source_id,
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
    shown = {"results": hits, "coverage": [c.model_dump(mode="json") for c in response.coverage]}
    return json.dumps({**shown, "note": note} if note else shown)


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _validation_message(exc: ValidationError) -> str:
    problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
    return _error(f"invalid search arguments: {problems}")
