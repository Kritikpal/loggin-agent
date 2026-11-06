"""overview: how many events, and of what kinds (spec 2026-11-03 §1). Replaces summarize_logs.

Events are grouped by signature (app.ingestion.signature), largest kind first, each with its first and last event as
a citation (`L3079@10:49Z`), so a count can be cited without another call. Read-only SELECTs, every value bound.
"""

import logging
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from langchain_core.tools import StructuredTool
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.format import coverage_line, short
from app.agent.tools.common import (
    END_HELP, TIME_HELP, Level, ToolArgs, fitted, hidden_text, levels_from, project_timezone, severity_sql,
    time_window, tool,
)
from app.core.database import SessionLocal
from app.ingestion.coverage import coverage_for_all
from app.ingestion.projects import Project

logger = logging.getLogger(__name__)

DEFAULT_TOP = 10
MAX_TOP = 25
TEMPLATE_HEAD = 90  # kinds often share a long prefix ("Error while updating attributes of user: <*> ..."),
TEMPLATE_TAIL = 40  # so the tail is kept too: that is where they differ
_HEAD = re.compile(r"^\S+ (\{[^}]*\}) - ")  # "ERROR {Logger} - " -> "{Logger} ": the level has its own column

BUDGET = 1  # calls per question (the registry in app.agent.tools enforces it; the model is told in DESCRIPTION)

DESCRIPTION = f"""\
Count events by kind (sig) in a window, most severe then largest first, each kind with its first and last event \
(L<line>@<time>) to cite. Use first for how many, what kinds, is anything wrong, first or last, and why something \
fails: the largest error kinds are the causes to follow with find(sig). The source of counts. \
{BUDGET} call per question."""


class OverviewArgs(ToolArgs):
    start_time: datetime | None = Field(default=None, description=TIME_HELP)
    end_time: datetime | None = Field(default=None, description=END_HELP)
    min_level: Level = Field(default="INFO", description="Lowest level counted (default INFO)")
    by_day: bool = Field(default=False, description="Also count per day")
    top: int = Field(default=DEFAULT_TOP, ge=1)  # no le: the provider would reject the call before our code runs


@dataclass(frozen=True)
class Scope:
    """The rows one overview or find covers: the project's files, a time window and the levels shown."""

    source_ids: tuple[str, ...]
    start: datetime | None
    end: datetime | None
    levels: tuple[str, ...] | None  # None = every level

    def where(self, *, levels: bool = True) -> tuple[str, dict]:
        conditions, params = ["source_id = any(:ids)"], {"ids": list(self.source_ids)}
        if self.start:
            conditions.append("occurred_at >= :start")
            params["start"] = self.start
        if self.end:
            conditions.append("occurred_at <= :end")
            params["end"] = self.end
        if levels and self.levels is not None:
            conditions.append("level = any(:levels)")
            params["levels"] = list(self.levels)
        return " and ".join(conditions), params


def make_overview_tool(project: Project) -> StructuredTool:
    """Only the project's files are counted. Days are counted in the project's timezone."""
    sources, project_id = project.sources, project.id

    async def run(**arguments) -> str:
        start, end = time_window(arguments, sources)
        top = min(arguments.get("top", DEFAULT_TOP), MAX_TOP)
        try:
            async with SessionLocal() as session:
                by_level = await level_counts(session, Scope(tuple(s.id for s in sources), start, end, None))
                shown = levels_from(arguments.get("min_level", "INFO"), list(by_level))
                scope = Scope(tuple(s.id for s in sources), start, end, tuple(shown))
                kinds = await kind_rows(session, scope, top)
                days = await day_counts(session, scope, project_timezone(sources)) if arguments.get("by_day") else []
                coverage = await coverage_for_all(session, sources)
        except Exception:
            logger.exception("overview tool failed")
            return "error: overview is temporarily unavailable"
        return render(kinds, days, by_level, shown, (start, end), project_id, coverage_line(coverage, project_id))

    return tool(run, "overview", DESCRIPTION, OverviewArgs)


async def level_counts(session: AsyncSession, scope: Scope) -> dict[str | None, int]:
    where, params = scope.where(levels=False)
    rows = await session.execute(text(f"select level, count(*) from log_events where {where} group by level"), params)
    return dict(rows.all())


async def kind_rows(session: AsyncSession, scope: Scope, top: int) -> list:
    """The `top` kinds, most severe level first, then largest, with the total number of kinds and each kind's first
    and last event. Severity first: by count alone, "is anything wrong?" led with 176 routine TLD-scan lines."""
    where, params = scope.where()
    order = "occurred_at {d} nulls last, source_id {d}, line {d}"
    sql = f"""
        select signature as sig, level, count(*) as n, count(*) over () as kinds,
               count(distinct source_id) as files, min(signature_template) as template,
               min(occurred_at) as first_at, max(occurred_at) as last_at,
               (array_agg(source_id order by {order.format(d='asc')}))[1] as first_file,
               (array_agg(line order by {order.format(d='asc')}))[1] as first_line,
               (array_agg(source_id order by {order.format(d='desc')}))[1] as last_file,
               (array_agg(line order by {order.format(d='desc')}))[1] as last_line
        from (select *, (locator->>'line_start')::int as line from log_events where {where}) e
        group by signature, level
        order by {severity_sql()} desc, n desc, first_at nulls last, sig
        limit :top"""
    return list((await session.execute(text(sql), {**params, "top": top})).all())


async def day_counts(session: AsyncSession, scope: Scope, timezone: str) -> list:
    where, params = scope.where()
    sql = f"""
        select signature as sig, to_char(occurred_at at time zone :tz, 'MM-DD') as day, count(*) as n
        from log_events where {where} and occurred_at is not null
        group by 1, 2 order by 2"""
    return list((await session.execute(text(sql), {**params, "tz": timezone})).all())


def render(kinds: Sequence, days: Sequence, by_level: dict, shown: Sequence[str], window: tuple, project_id: str,
           coverage: str) -> str:
    total = sum(n for level, n in by_level.items() if level in shown)
    per_level = " · ".join(f"{level} {n}" for level, n in sorted(by_level.items(), key=lambda kv: -kv[1])
                           if level in shown)
    times = [t for k in kinds for t in (k.first_at, k.last_at) if t]
    with_day = len({t.astimezone(UTC).date() for t in times}) > 1
    files = {f for k in kinds for f in (k.first_file, k.last_file)}
    head = [f"window {window_text(window)}", f"total {total}", *([per_level] if per_level else []),
            hidden_text(by_level, shown)]
    if days:
        totals: dict[str, int] = defaultdict(int)
        for d in days:
            totals[d.day] += d.n
        head.append("days " + ", ".join(f"{day} {n}" for day, n in totals.items()))
    if len(files) == 1:
        head.append(f"file {short(next(iter(files)), project_id)}")
    header = " · ".join(head) + " · columns: sig count level first last template"
    per_kind: dict[str, list[str]] = defaultdict(list)
    for d in days:
        per_kind[d.sig].append(f"{d.day} {d.n}")
    lines = []
    for k in kinds:
        many = k.files > 1
        lines.append(" ".join([
            k.sig or "-", str(k.n), (k.level or "-")[:16],
            cite(k.first_file, k.first_line, k.first_at, many, with_day, project_id),
            cite(k.last_file, k.last_line, k.last_at, many, with_day, project_id), template_text(k.template or "")]))
        if days:
            lines.append("  per day: " + ", ".join(per_kind.get(k.sig, [])))
    more = (kinds[0].kinds - len(kinds)) if kinds else 0
    note = f"{more} more kinds; raise top (max {MAX_TOP}) or narrow by time or level" if more > 0 else None
    return fitted(header, lines, note, coverage, what="lines")


def template_text(template: str) -> str:
    """The kind's text without its level, head and tail kept when long, so kinds sharing a prefix stay apart."""
    body = _HEAD.sub(r"\1 ", template, count=1)
    if len(body) <= TEMPLATE_HEAD + TEMPLATE_TAIL + 1:
        return body
    return f"{body[:TEMPLATE_HEAD]}…{body[-TEMPLATE_TAIL:]}"


def cite(file: str, line: int | None, at: datetime | None, with_file: bool, with_day: bool, project_id: str) -> str:
    where = f"{short(file, project_id)}:" if with_file else ""
    when = "-" if at is None else f"{at.astimezone(UTC):%m-%dT%H:%MZ}" if with_day else f"{at.astimezone(UTC):%H:%MZ}"
    return f"{where}L{line if line is not None else '?'}@{when}"


def window_text(window: tuple[datetime | None, datetime | None]) -> str:
    start, end = window
    if start is None and end is None:
        return "all"
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return f"{start.strftime(fmt) if start else '…'}..{end.strftime(fmt) if end else '…'}"
