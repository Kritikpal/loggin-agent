"""count_by: exact counts of events by one or two fields (spec 2026-11-06), for "how many / who / which the most / per day".

The model chooses names from a fixed vocabulary; each name maps to a SQL expression below, so no model text is ever part of
a statement. Every value (filters, window, timezone, limit) is a bound parameter. Read-only SELECTs over the project's own
sources. The `audit_*` fields exist for WSO2 audit events (app.ingestion.fields.audit); `level`, `day` and `hour` work for
any event.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from langchain_core.tools import StructuredTool
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.common import END_HELP, TIME_HELP, ToolArgs, fitted, project_timezone, time_window, tool
from app.agent.tools.format import coverage_line
from app.agent.tools.overview import Scope, cite, window_text
from app.core.database import SessionLocal
from app.ingestion.coverage import coverage_for_all
from app.ingestion.projects import Project

logger = logging.getLogger(__name__)

# name -> SQL over log_events. Constants only. The day/hour patterns hold a colon, escaped for text() ("\:").
DIMENSIONS: Mapping[str, str] = {
    "action": "fields->>'audit_action'",
    "result": "fields->>'audit_result'",
    "actor": "fields->>'audit_actor'",
    "target": "fields->>'audit_target'",
    "application": "fields->>'audit_application'",
    "user": "coalesce(fields->>'audit_user', fields->>'audit_actor')",
    "level": "level",
    "day": "to_char(occurred_at at time zone :tz, 'YYYY-MM-DD')",
    "hour": "to_char(occurred_at at time zone :tz, 'YYYY-MM-DD HH24\\:00')",
}
FILTERS = ("action", "result", "application", "user")
BUDGET = 2  # calls per question (the registry in app.agent.tools enforces it; the model is told in DESCRIPTION)
DEFAULT_TOP = 15
MAX_TOP = 50
HIDDEN_NOTE = ("[REDACTED:email:u...] is one person whose address is hidden; the same code is the same person; "
               "a plain [REDACTED:email] is an unknown person")

DESCRIPTION = (
    "Count events by one or two fields: user, application, action, result, day, hour, level, target, actor. Use for "
    "how many logins or failures, who or which application the most, how many different users (the groups figure) "
    "and a report per day. Filter by action, result, application or user. Counts are exact: never count listed "
    "events yourself. An event is one log line, not a session. "
    f"{BUDGET} calls per question."
)

Dimension = Literal["action", "result", "user", "actor", "application", "target", "level", "day", "hour"]
_ORDER = "occurred_at {d} nulls last, source_id {d}, line {d}"


@dataclass(frozen=True)
class Group:
    key: tuple[str, ...]
    n: int
    first_file: str
    first_line: int | None
    first_at: datetime | None
    last_file: str
    last_line: int | None
    last_at: datetime | None


@dataclass(frozen=True)
class Counts:
    total: int  # events counted in the groups: they match the filters and have every chosen field
    groups: int  # distinct groups (so "how many different users" is this number)
    missing: int  # events that match the filters but lack a chosen field: not a group
    rows: tuple[Group, ...]  # the `top` largest groups


async def group_counts(
    session: AsyncSession, *, source_ids: Sequence[str], by: Sequence[str], filters: Mapping[str, str],
    start: datetime | None, end: datetime | None, timezone: str, top: int,
) -> Counts:
    if not by or any(name not in DIMENSIONS for name in by) or any(name not in FILTERS for name in filters):
        raise ValueError("unknown dimension or filter")
    where, params = Scope(tuple(source_ids), start, end, None).where()
    conditions = [where]
    for name, value in filters.items():  # the SQL names the filter by constant; the value is bound
        conditions.append(f"lower({DIMENSIONS[name]}) = lower(:f_{name})")
        params[f"f_{name}"] = value
    where = " and ".join(conditions)
    params["tz"] = timezone
    keys = [f"k{i}" for i in range(len(by))]
    key_columns = ", ".join(keys)
    inner = (f"select source_id, occurred_at, (locator->>'line_start')::int as line, "
             + ", ".join(f"{DIMENSIONS[name]} as k{i}" for i, name in enumerate(by))
             + f" from log_events where {where}")
    present = " and ".join(f"{key} is not null" for key in keys)
    absent = " or ".join(f"{key} is null" for key in keys)
    grouped = text(f"""
        select {key_columns}, n, first_file, first_line, first_at, last_file, last_line, last_at,
               count(*) over () as groups, sum(n) over () as total
        from (
            select {key_columns}, count(*) as n,
                   (array_agg(source_id order by {_ORDER.format(d='asc')}))[1] as first_file,
                   (array_agg(line order by {_ORDER.format(d='asc')}))[1] as first_line,
                   min(occurred_at) as first_at,
                   (array_agg(source_id order by {_ORDER.format(d='desc')}))[1] as last_file,
                   (array_agg(line order by {_ORDER.format(d='desc')}))[1] as last_line,
                   max(occurred_at) as last_at
            from ({inner}) e where {present} group by {key_columns}
        ) g order by n desc, {key_columns} limit :top""")
    rows = (await session.execute(grouped, {**params, "top": top})).all()
    missing = await session.scalar(
        text(f"select count(*) from (select {key_columns} from ({inner}) e) m where {absent}"), params)
    groups = tuple(
        Group(tuple(r[i] for i in range(len(keys))), r.n, r.first_file, r.first_line, r.first_at, r.last_file,
              r.last_line, r.last_at) for r in rows)
    return Counts(int(rows[0].total) if rows else 0, int(rows[0].groups) if rows else 0, int(missing or 0), groups)


class CountByArgs(ToolArgs):
    by: list[Dimension] = Field(min_length=1, max_length=2, description="What to group by; two make a cross table")
    action: str | None = Field(default=None, description="Only this action, e.g. Login")
    result: str | None = Field(default=None, description="Only this result, e.g. Success or Failed")
    application: str | None = Field(default=None, description="Only this application")
    user: str | None = Field(default=None, description="Only this user")
    start_time: datetime | None = Field(default=None, description=TIME_HELP)
    end_time: datetime | None = Field(default=None, description=END_HELP)
    top: int = Field(default=DEFAULT_TOP, ge=1)  # no le: the provider would reject the call before our code runs


def make_count_by_tool(project: Project) -> StructuredTool:
    """Only the project's own sources are counted. Days and hours are in the project's timezone."""
    sources, project_id = project.sources, project.id

    async def run(**arguments) -> str:
        start, end = time_window(arguments, sources)
        top = min(arguments.get("top", DEFAULT_TOP), MAX_TOP)
        by = list(arguments["by"])
        filters = {name: arguments[name] for name in FILTERS if arguments.get(name)}
        try:
            async with SessionLocal() as session:
                counts = await group_counts(
                    session, source_ids=[s.id for s in sources], by=by, filters=filters, start=start, end=end,
                    timezone=project_timezone(sources), top=top)
                coverage = await coverage_for_all(session, sources)
        except Exception:
            logger.exception("count_by tool failed")
            return "error: count_by is temporarily unavailable"
        return render_counts(counts, by=by, filters=filters, window=(start, end), project_id=project_id,
                             coverage=coverage_line(coverage, project_id), top=top)

    return tool(run, "count_by", DESCRIPTION, CountByArgs)


def render_counts(counts: Counts, *, by: Sequence[str], filters: Mapping[str, str],
                  window: tuple[datetime | None, datetime | None], project_id: str, coverage: str, top: int) -> str:
    what = " / ".join(by)
    if counts.total == 0 and counts.missing > 0:
        return (f"no events in this window have {what} ({counts.missing} events match the filters but lack it)"
                f"\n{coverage}")
    head = [f"window {window_text(window)}",
            "filters " + (" ".join(f"{k}={v}" for k, v in filters.items()) or "none"),
            f"by {', '.join(by)}", f"total {counts.total}", f"groups {counts.groups}"]
    if counts.missing:
        head.append(f"{counts.missing} events have no {what}")
    header = " · ".join(head) + " · columns: " + " | ".join([*by, "count", "first", "last"])
    many_files = len({f for g in counts.rows for f in (g.first_file, g.last_file)}) > 1
    lines = [" | ".join([*g.key, str(g.n), cite(g.first_file, g.first_line, g.first_at, many_files, True, project_id),
                         cite(g.last_file, g.last_line, g.last_at, many_files, True, project_id)])
             for g in counts.rows]
    notes = []
    if counts.groups > len(counts.rows):
        notes.append(f"{counts.groups - len(counts.rows)} more groups; raise top (max {MAX_TOP}) or narrow the window")
    if any(part.startswith("[REDACTED:email") for g in counts.rows for part in g.key):
        notes.append(HIDDEN_NOTE)
    return fitted(header, lines, "; ".join(notes) or None, coverage, what="rows")
