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
MAX_TOP_K_FILTERED = 20  # a level filter is how the model lists every ERROR or WARN, so it may see more
FILTER_ONLY_QUERY = "error"  # stands in when the model searches by filter alone; the filters do the narrowing
MAX_RAW_TEXT = 800  # characters of one event shown to the model

DESCRIPTION = """\
Search the indexed application logs. Read-only. Returns the best-matching events (raw_text, source, line, time, level,
matched_by) plus `coverage`, how complete the index is per source. It never answers the question; you do.
A very long event (a stack trace) is shortened to its start and end; the middle is marked "chars omitted".

Choose the arguments from the question:
- Identifier (error code, class or bean name, id such as HHH000342): put the token alone in `query`, top_k 5.
- Meaning ("why did startup fail?"): the question in plain words. Without `levels` at most 5 hits are shown, however
  large top_k is; with `levels` up to 20.
- Mixed: plain words plus the identifier exactly as it appears in the log.
- By severity ("show the errors"): `levels` ["ERROR"], a generic query such as "error", top_k 20.
- By time: `start_time` and `end_time` in UTC (convert from the source's timezone).
- By application: `source_ids`, only when the user named that application or source. Otherwise leave it out and
  search all sources; never guess which one the question is about.
Do not add quotes or operators to `query`. Results are always returned, even for an unrelated question: judge
relevance yourself. matched_by with both "semantic" and "keyword" is strong evidence; "semantic" alone is a weak lead.
Scores are rank-based; never compare them across queries. An empty result only means "nothing found" when coverage
is "complete"."""


class AgentSearchArgs(SearchLogsRequest):
    """The API's request with a smaller default top_k, so the model gets (and pays for) fewer hits unless it asks."""

    # No min_length: the provider rejects an empty query before our code runs, and the model sends one when it
    # searches by level, time or source alone. run() swaps it for FILTER_ONLY_QUERY.
    query: str = Field(max_length=2000, description="Natural-language search query; a short generic word such as "
                                                    "'error' is fine when searching only by level, time or source")
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=50, description=SearchLogsRequest.model_fields["top_k"].description)


def make_search_logs_tool(
    *,
    session_factory: Callable = SessionLocal,
    load_sources: Callable[[], tuple[Source, ...]] = get_sources,
    embed: Callable[[str], list[float]] = embed_query,
) -> StructuredTool:
    async def run(**arguments) -> str:
        request = SearchLogsRequest(**{**arguments, "query": arguments["query"].strip() or FILTER_ONLY_QUERY})
        requested, cap = request.top_k, MAX_TOP_K_FILTERED if request.levels else MAX_TOP_K
        request = request.model_copy(update={"top_k": min(requested, cap)})
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

SUMMARY_DESCRIPTION = """\
Count the indexed log events per source and level, with the first and last event time of each group, plus
`coverage`. Read-only, returns no log text, and costs no search. Use it for "how many", "when did X start / stop" and
open questions ("is anything wrong?") to see where to search; skip it when the question names an identifier.
Filters work as in search_logs: `levels`, `start_time` / `end_time` in UTC, and `source_ids` only when the user named
the source."""


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
    hits = [
        {
            "source_id": hit.source_id,
            "occurred_at": hit.occurred_at.isoformat() if hit.occurred_at else None,
            "level": hit.level,
            "line_start": hit.locator.get("line_start"),
            "line_end": hit.locator.get("line_end"),
            "raw_text": _shorten(hit.raw_text),
            "score": round(hit.score, 4),
            "matched_by": hit.matched_by,
        }
        for hit in response.results
    ]
    shown = {"results": hits, "coverage": [c.model_dump(mode="json") for c in response.coverage]}
    return json.dumps({**shown, "note": note} if note else shown)


def _shorten(text: str) -> str:
    """Keep the start and the end: a trace's cause ("Caused by: ...") is at the bottom."""
    if len(text) <= MAX_RAW_TEXT:
        return text
    head, tail = MAX_RAW_TEXT * 2 // 3, MAX_RAW_TEXT // 3
    return f"{text[:head]}\n[... {len(text) - head - tail} chars omitted ...]\n{text[-tail:]}"


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _validation_message(exc: ValidationError) -> str:
    problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
    return _error(f"invalid search arguments: {problems}")
