"""What the agent's tools share: the tool wrapper, time and level handling, and the size cut.

Tools are called in-process (no HTTP). Every failure goes back to the model as text (`error: <what to fix>`) instead
of being raised: it can fix a bad argument and retry. An argument error is a ToolException (status "error"), which
the budget middleware does not count; unexpected errors are logged in full, reported in general terms, and do count.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, ValidationError

from app.agent.tools.format import assemble, fit
from app.ingestion.sources import Source

# Most to least verbose. Aliases rank with their level, so a project logging WARNING or FATAL still filters right.
LEVEL_RANKS = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "WARNING": 3, "ERROR": 4, "SEVERE": 4, "FATAL": 5,
               "CRITICAL": 5}
Level = Literal["DEBUG", "INFO", "WARN", "ERROR"]
TIME_HELP = "ISO 8601 with offset, e.g. 2026-10-30T18:35:00+05:30"



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
