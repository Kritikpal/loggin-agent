# TODO: SECURITY — run llm-secure-patterns to apply LLM security patterns
"""What the agent's tools share, and search_logs (events similar in meaning). overview and find live in
app.agent.overview and app.agent.find; app.agent.registry lists all three with their budgets.

Tools are called in-process (no HTTP) with their collaborators injected, so tests can point them at the test database
and a fake embedder. Every failure goes back to the model as text (`error: <what to fix>`) instead of being raised:
it can fix a bad argument and retry, and the graph never crashes on a tool call. An argument error is a ToolException
(status "error"), which the budget middleware does not count; unexpected errors are logged in full, reported in general
terms, and do count, since the call ran.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.rows import Row, assemble, coverage_line, event_rows, fit, row_text, short
from app.core.database import SessionLocal
from app.core.embeddings import embed_query
from app.ingestion.sources import Source
from app.schemas.search_logs import SearchLogsRequest, SearchLogsResponse
from app.tools.search_logs import search_logs

logger = logging.getLogger(__name__)

# Most to least verbose. Aliases rank with their level, so a project logging WARNING or FATAL still filters right.
LEVEL_RANKS = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "WARNING": 3, "ERROR": 4, "SEVERE": 4, "FATAL": 5,
               "CRITICAL": 5}
Level = Literal["DEBUG", "INFO", "WARN", "ERROR"]
TIME_HELP = "ISO 8601 with offset, e.g. 2026-10-30T18:35:00+05:30"

# Groq's free tier allows 8,000 tokens a minute, and every result is sent again with each later model call.
DEFAULT_TOP_K = 5
MAX_TOP_K = 5  # hits shown for a search without a level filter, whatever top_k says
MAX_TOP_K_FILTERED = 10  # a level filter is how the model lists every ERROR or WARN; 20 made one 9,100-token request
FILTER_ONLY_QUERY = "error"  # stands in when the model searches by filter alone; the filters do the narrowing
MAX_RAW_TEXT = 800  # characters of one hit's message; 10 hits get 450 (ADR-003)
MIN_MESSAGE = 450  # never less: a WSO2 error line is 417 characters and its user id sits at character 334
MAX_HIT_CAUSE = 200
HITS_TEXT = 4000

# Sent with every model call (ADR-003 layer 2): only what the tool shows and how to fill `query`.
DESCRIPTION = """\
Events similar in meaning, when the exact words are unknown. Shows at most 5 hits, 10 with `levels`: a sample, never \
count them. `query`: an identifier, plain words or both; no quotes or operators. `by`: k keyword, s semantic match."""


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No min_length: the provider rejects an empty query before our code runs; run() swaps it for FILTER_ONLY_QUERY.
    query: str = Field(max_length=2000, description="Search text")
    # No le: the provider rejects a too-large top_k before our code runs (HTTP 400, the whole question fails).
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1)
    start_time: datetime | None = Field(default=None, description=TIME_HELP)
    end_time: datetime | None = Field(default=None, description=TIME_HELP)
    levels: list[str] | None = Field(default=None, description="e.g. ERROR, WARN")


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
        sources = load_sources()
        start, end = time_window(arguments, sources)
        try:
            request = SearchLogsRequest(
                query=arguments["query"].strip() or FILTER_ONLY_QUERY, top_k=min(requested, cap),
                start_time=start, end_time=end, levels=arguments.get("levels"))
        except ValidationError as exc:
            raise ToolException(validation_text(exc)) from exc
        try:
            async with session_factory() as session:
                response = await search_logs(request, session, sources=sources, embed=embed)
        except Exception:
            logger.exception("search_logs tool failed")
            return "error: search is temporarily unavailable"
        note = f"showing the top {cap} of the {requested} requested hits" if requested > cap else None
        return compact_hits(response, project_id, note, sources)

    return tool(run, "search_logs", DESCRIPTION, SearchArgs)


def compact_hits(
    response: SearchLogsResponse, project_id: str, note: str | None = None, sources: Sequence[Source] = (),
) -> str:
    """Hits as find-style rows plus their sig, so a good hit can be followed by find(sig=...). A stack trace can be
    20,000 characters; each hit keeps its message (450-800 characters) and the last "Caused by"."""
    by_id = {s.id: s for s in sources}
    message_max = max(MIN_MESSAGE, min(MAX_RAW_TEXT, HITS_TEXT // max(len(response.results), 1)))
    rows = [
        Row(file=short(hit.source_id, project_id), occurred_at=hit.occurred_at, line=hit.locator.get("line_start"),
            level=hit.level, sig=hit.signature, extra="".join(leg[0] for leg in hit.matched_by),
            text=row_text(hit.raw_text, by_id.get(hit.source_id), limit=message_max, cause=MAX_HIT_CAUSE))
        for hit in response.results
    ]
    header, lines = event_rows(rows, "time line level sig by text", total_label="hits", total=len(rows))
    return assemble(header, lines, note, coverage_line(response.coverage, project_id))


# --- shared by the three tools ----------------------------------------------------------------------------------


def tool(run: Callable, name: str, description: str, schema: type[BaseModel]) -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=run, name=name, description=description, args_schema=schema,
        handle_validation_error=lambda exc: f"error: {validation_text(exc)}",
        handle_tool_error=lambda exc: f"error: {exc}",
    )


def project_timezone(sources: Sequence[Source]) -> str:
    return sources[0].timezone if sources else "UTC"  # one project, one timezone


def to_utc(at: datetime | None, timezone: str) -> datetime | None:
    """A time with an offset is converted; one without is read in the project's timezone (spec: times in)."""
    if at is None:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=ZoneInfo(timezone))).astimezone(UTC)


def time_window(arguments: dict, sources: Sequence[Source]) -> tuple[datetime | None, datetime | None]:
    timezone = project_timezone(sources)
    start, end = to_utc(arguments.get("start_time"), timezone), to_utc(arguments.get("end_time"), timezone)
    if start and end and start > end:
        raise ToolException("start_time must not be after end_time")
    return start, end


def levels_from(min_level: str | None, present: Sequence[str | None]) -> list[str] | None:
    """The levels at or above `min_level` among those present; None = no filter."""
    if min_level is None:
        return None
    floor = LEVEL_RANKS[min_level]
    return [level for level in present if level and LEVEL_RANKS.get(level.upper(), floor) >= floor]


def hidden_text(by_level: dict[str | None, int], shown: Sequence[str] | None) -> str:
    """`hidden DEBUG 2754` for the levels a min_level left out, or `hidden none`."""
    if shown is None:
        return "hidden none"
    hidden = [f"{level or 'unlevelled'} {n}" for level, n in sorted(
        by_level.items(), key=lambda kv: -kv[1]) if level not in shown]
    return "hidden " + (", ".join(hidden) if hidden else "none")


def validation_text(exc: ValidationError) -> str:
    problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
    return f"invalid arguments: {problems}"


def severity_sql(column: str = "level") -> str:
    """LEVEL_RANKS as a SQL expression; a level it does not know ranks lowest. Built from constants only."""
    cases = " ".join(f"when '{level}' then {rank}" for level, rank in LEVEL_RANKS.items())
    return f"(case upper({column}) {cases} else -1 end)"


def fitted(header: str, lines: list[str], note: str | None, coverage: str, *, what: str,
           priority: Sequence[int] | None = None) -> str:
    """The result, cut to MAX_RESULT_CHARS by dropping rows (lowest `priority` first); a cut is added to the note."""
    kept, cut = fit(header, lines, [coverage, note or ""], what=what, priority=priority)
    return assemble(header, kept, "; ".join(filter(None, [note, cut])) or None, coverage)
