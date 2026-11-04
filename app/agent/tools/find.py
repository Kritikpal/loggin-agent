"""find: every event of a kind or with exact text, with the total; with only a time window, the timeline (spec
2026-11-03 §2). Read-only SELECTs, every value bound.

In window mode, consecutive events of one kind less than REPEAT_GAP apart fold into one row (`×11 until 13:14:54Z`).
The folding is done in SQL (gaps and islands over the time-ordered events): the busy 15 minutes around a restart hold
2,861 events, and only the rows shown should leave the database.
"""

import logging
from datetime import datetime, timedelta

from langchain_core.tools import StructuredTool, ToolException
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.overview import Scope, level_counts, window_text
from app.agent.tools.format import Row, coverage_line, event_rows, row_text, short
from app.agent.tools.common import (
    END_HELP, LEVEL_RANKS, TIME_HELP, Level, ToolArgs, fitted, hidden_text, levels_from, severity_sql, time_window,
    tool,
)
from app.core.database import SessionLocal
from app.ingestion.coverage import coverage_for_all
from app.ingestion.projects import Project
from app.ingestion.sources import Source

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 10
MAX_LIMIT = 25  # also the default for a timeline: its errors and lifecycle steps need the room
MAX_WINDOW = timedelta(hours=2)  # a timeline is for "what happened at T"; a day of events needs overview
REPEAT_GAP = timedelta(seconds=60)
MAX_TEXT = 200
LIFECYCLE = r"\y(start(ed|ing)?|shut ?down|shutting down|stopp(ed|ing)|restart(ed|ing)?)\y"  # PostgreSQL ARE

BUDGET = 2  # calls per question (the registry in app.agent.tools enforces it; the model is told in DESCRIPTION)

DESCRIPTION = f"""\
Events, oldest first, with the total: every event of a kind (sig) or containing exact text (any case, stack traces \
included); with neither, the timeline of a window of at most 2 hours, repeats folded (×n until time). Use for exact \
words, an identifier, a sig from an earlier result, or what happened at a time. The total is the count. If the \
total is far larger than the rows shown, the text is too broad: narrow with min_level or a sig. \
{BUDGET} calls per question."""


class FindArgs(ToolArgs):
    sig: str | None = Field(default=None, description="From an earlier result")
    text: str | None = Field(default=None, max_length=MAX_TEXT, description="Exact words, any case")
    start_time: datetime | None = Field(default=None, description=TIME_HELP)
    end_time: datetime | None = Field(default=None, description=END_HELP)
    min_level: Level | None = Field(default=None, description="Only if asked for errors; failures are often WARN")
    limit: int | None = Field(default=None, ge=1, description="Default 10, 25 for a window; max 25")


def make_find_tool(project: Project) -> StructuredTool:
    sources, project_id = project.sources, project.id  # only the project's sources: that keeps a chat inside its project

    async def run(**arguments) -> str:
        sig, needle = (arguments.get("sig") or "").strip().lower(), (arguments.get("text") or "").strip()
        if sig and needle:
            raise ToolException("give sig or text, not both")
        start, end = time_window(arguments, sources)
        window = not sig and not needle
        if window and (start is None or end is None or end - start > MAX_WINDOW):
            raise ToolException("without sig or text, give start_time and end_time at most 2 hours apart")
        limit = min(arguments.get("limit") or (MAX_LIMIT if window else DEFAULT_LIMIT), MAX_LIMIT)
        ids = tuple(s.id for s in sources)
        fallback = False
        try:
            async with SessionLocal() as session:
                if sig and not await _known(session, ids, sig):
                    raise ToolException(f"unknown sig '{sig}'; copy one from an earlier result")
                by_level = await level_counts(session, Scope(ids, start, end, None))
                min_level = arguments.get("min_level") or ("INFO" if window else None)
                shown = levels_from(min_level, list(by_level))
                scope = Scope(ids, start, end, None if shown is None else tuple(shown))
                found = await (_timeline(session, scope, limit) if window else _matches(session, scope, sig, needle, limit))
                if not window and not found[0] and shown is not None:  # a miss under a level filter may not be absence
                    wider = await _matches(session, Scope(ids, start, end, None), sig, needle, limit)
                    if wider[0]:
                        found, fallback = wider, True
                coverage = await coverage_for_all(session, sources)
        except ToolException:
            raise
        except Exception:
            logger.exception("find tool failed")
            return "error: find is temporarily unavailable"
        total, rows = found
        by_id = {s.id: s for s in sources}
        events = [_row(r, by_id.get(r.source_id), project_id, needle) for r in rows]
        header, lines = event_rows(events, "time line level sig text", total_label="rows" if window else "total",
                                   total=total)
        if window:
            header = f"window {window_text((start, end))} · events {sum(by_level.values())} · " \
                     f"{hidden_text(by_level, shown)} · {header}"
        if fallback:
            more = f", showing {len(rows)} of {total}" if total > len(rows) else ""
            note = f"none at {min_level} or worse; these are at lower levels{more}"
        elif total <= len(rows):
            note = None
        elif window:
            note = (f"{total - len(rows)} of {total} runs left out (every WARN and ERROR, then start/stop, then the "
                    "rest spread over the window); narrow with a shorter window or min_level WARN")
        else:
            note = f"showing the oldest {len(rows)} of {total}; narrow by time, level or sig"
        # a size cut drops a timeline's least important tier first; elsewhere the latest rows go first
        priority = [r.tier for r in rows] if window else None
        return fitted(header, lines, note, coverage_line(coverage, project_id), what="rows", priority=priority)

    return tool(run, "find", DESCRIPTION, FindArgs)


async def _known(session: AsyncSession, ids: tuple[str, ...], sig: str) -> bool:
    found = await session.execute(
        text("select 1 from log_events where source_id = any(:ids) and signature = :sig limit 1"),
        {"ids": list(ids), "sig": sig})
    return found.first() is not None


_COLUMNS = "source_id, occurred_at, level, signature, (locator->>'line_start')::int as line, raw_text"


async def _matches(session: AsyncSession, scope: Scope, sig: str, needle: str, limit: int) -> tuple[int, list]:
    """Events with this signature or containing this text anywhere (stack traces too), oldest first."""
    where, params = scope.where()
    if sig:
        where, params = f"{where} and signature = :sig", {**params, "sig": sig}
    else:
        # ponytail: a sequential scan of raw_text, fine for tens of thousands of events; a pg_trgm index if it grows
        where, params = f"{where} and strpos(lower(raw_text), lower(:needle)) > 0", {**params, "needle": needle}
    sql = f"""select {_COLUMNS}, count(*) over () as total, 1 as repeat, null::timestamptz as until
              from log_events where {where}
              order by occurred_at nulls last, source_id, line limit :limit"""
    rows = list((await session.execute(text(sql), {**params, "limit": limit})).all())
    return (rows[0].total if rows else 0), rows


async def _timeline(session: AsyncSession, scope: Scope, limit: int) -> tuple[int, list]:
    """The window's events with runs of one kind folded. Runs are per kind: an event joins its kind's run when it
    follows that kind's previous event by less than REPEAT_GAP, even with other kinds in between (a restart logs 11
    TLD scans over 3 minutes, interleaved with everything else starting up). Only shown levels take part, so hidden
    DEBUG lines never split a run. Each run is shown as its first event, with its size and last time.

    When there are more runs than `limit`, the first N were a shutdown's routine steps and hid the restart and its
    errors. So the rows are chosen in tiers, then shown in time order:
      2  every WARN-or-worse run, most severe then earliest
      1  lifecycle runs (start, stop, shutdown, restart: what "what happened at T" is usually about)
      0  the other runs
    Tiers 1 and 0 are each spread evenly over the window (first and last included) in the slots left to them."""
    where, params = scope.where()
    order = "occurred_at, source_id, line"
    sql = f"""
        with ev as (
            select id, occurred_at, source_id, signature, (locator->>'line_start')::int as line,
                   {severity_sql()} as severity, coalesce(signature_template ~* :lifecycle, false) as lifecycle
            from log_events where {where}
        ), marked as (
            select *, case when occurred_at - lag(occurred_at) over w < :gap then 0 else 1 end as starts
            from ev window w as (partition by signature order by {order})
        ), runs as (
            select *, sum(starts) over (partition by signature order by {order} rows unbounded preceding) as run
            from marked
        ), folded as (
            select (array_agg(id order by {order}))[1] as id, min(occurred_at) as first_at, count(*) as repeat,
                   max(occurred_at) as until,
                   case when max(severity) >= :severe then 2 when bool_or(lifecycle) then 1 else 0 end as tier,
                   max(severity) as severity
            from runs group by signature, run
        ), numbered as (
            select *, count(*) over () as total,
                   row_number() over (partition by tier order by severity desc, first_at, id) as by_rank,
                   row_number() over (partition by tier order by first_at, id) as by_time
            from folded
        ), slots as (  -- how many rows each tier gets: the severe first, then lifecycle, then the rest
            select n1, n0, least(n1, :limit - least(n2, :limit)) as take1,
                   :limit - least(n2, :limit) - least(n1, :limit - least(n2, :limit)) as take0
            from (select count(*) filter (where tier = 2) as n2, count(*) filter (where tier = 1) as n1,
                         count(*) filter (where tier = 0) as n0 from folded) counts
        ), spread as (  -- positions spread evenly over a tier; with slots to spare, every position
            select 1 as tier, 1 + round(j * (n1 - 1)::numeric / greatest(take1 - 1, 1))::int as by_time
            from slots, generate_series(0, take1 - 1) as j
            union
            select 0, 1 + round(j * (n0 - 1)::numeric / greatest(take0 - 1, 1))::int
            from slots, generate_series(0, take0 - 1) as j
        )
        select {_COLUMNS}, f.total, f.repeat, f.until, f.tier
        from numbered f join log_events e on e.id = f.id
        where (f.tier = 2 and f.by_rank <= :limit) or (f.tier, f.by_time) in (select tier, by_time from spread)
        order by f.first_at, line"""
    values = {**params, "gap": REPEAT_GAP, "limit": limit, "severe": LEVEL_RANKS["WARN"], "lifecycle": LIFECYCLE}
    rows = list((await session.execute(text(sql), values)).all())
    return (rows[0].total if rows else 0), rows


def _row(r, source: Source | None, project_id: str, needle: str) -> Row:
    return Row(file=short(r.source_id, project_id), occurred_at=r.occurred_at, line=r.line, level=r.level,
               sig=r.signature, text=row_text(r.raw_text, source, needle=needle or None),
               repeat=(r.repeat, r.until) if r.repeat > 1 else None)
