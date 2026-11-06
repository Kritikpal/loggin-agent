"""count_by: exact counts of events by one or two fields (spec 2026-11-06), for "how many / who / which the most / per day".

The model chooses names from a fixed vocabulary; each name maps to a SQL expression below, so no model text is ever part of
a statement. Every value (filters, window, timezone, limit) is a bound parameter. Read-only SELECTs over the project's own
sources. The `audit_*` fields exist for WSO2 audit events (app.ingestion.fields.audit); `level`, `day` and `hour` work for
any event.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.overview import Scope

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
