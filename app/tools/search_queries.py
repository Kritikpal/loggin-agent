"""The SQL of search_logs: the two candidate legs and the final row fetch. Read-only SELECTs, every value bound.

Filters go INSIDE each leg's WHERE, so they apply before a leg takes its top candidates, never after a global top-k.

The semantic leg's ORDER BY embedding <=> :vector LIMIT n can be served by the approximate HNSW index. A plain HNSW
scan stops after hnsw.ef_search candidates and filters them afterwards, which can silently drop matching rows when a
filter is selective. pgvector 0.8's iterative scan keeps walking the graph until enough rows pass the WHERE clause
(up to hnsw.max_scan_tuples). relaxed_order may return rows slightly out of distance order; the caller re-sorts by
the returned distance, so that does not matter.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, Row, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.log_event import LogEvent
from app.schemas.search_logs import LogFilters

HNSW_EF_SEARCH = 100  # >= the candidate pool, so one graph pass can fill it
HNSW_ITERATIVE_SCAN = "relaxed_order"


@dataclass(frozen=True)
class Candidate:
    """One row a leg found, best first within its leg. `strength` is leg-specific: higher = better."""

    id: uuid.UUID
    occurred_at: datetime | None
    event_key: str
    strength: float


def filters(request: LogFilters, source_ids: Sequence[str]) -> list[ColumnElement[bool]]:
    conditions = [LogEvent.source_id.in_(list(source_ids))]
    if request.levels:
        conditions.append(LogEvent.level.in_(request.levels))
    if request.start_time is not None:
        conditions.append(LogEvent.occurred_at >= request.start_time)
    if request.end_time is not None:
        conditions.append(LogEvent.occurred_at <= request.end_time)
    return conditions


async def semantic_candidates(
    session: AsyncSession, vector: list[float], where: list[ColumnElement[bool]], limit: int,
) -> list[Candidate]:
    for name, value in (("hnsw.iterative_scan", HNSW_ITERATIVE_SCAN), ("hnsw.ef_search", str(HNSW_EF_SEARCH))):
        await session.execute(select(func.set_config(name, value, True)))  # true = local to this transaction
    distance = LogEvent.embedding.cosine_distance(vector)
    stmt = (
        select(LogEvent.id, LogEvent.occurred_at, LogEvent.event_key, distance.label("distance"))
        .where(*where, LogEvent.embedding.is_not(None))
        .order_by(distance)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    found = [Candidate(r.id, r.occurred_at, r.event_key, -float(r.distance)) for r in rows]
    return sorted(found, key=lambda c: -c.strength)


async def keyword_candidates(
    session: AsyncSession, tsquery: str, where: list[ColumnElement[bool]], limit: int,
) -> list[Candidate]:
    query = func.to_tsquery("simple", tsquery)
    rank = func.ts_rank(LogEvent.search_tsv, query)
    stmt = (
        select(LogEvent.id, LogEvent.occurred_at, LogEvent.event_key, rank.label("rank"))
        .where(*where, LogEvent.search_tsv.bool_op("@@")(query))
        .order_by(desc("rank"), LogEvent.occurred_at.desc().nulls_last(), LogEvent.event_key)
        .limit(limit)
    )
    return [Candidate(r.id, r.occurred_at, r.event_key, float(r.rank)) for r in (await session.execute(stmt)).all()]


async def fetch_events(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Row]:
    """The evidence columns of the chosen rows (not the vector or the search text)."""
    if not ids:
        return {}
    stmt = select(
        LogEvent.id, LogEvent.source_id, LogEvent.occurred_at, LogEvent.level, LogEvent.locator, LogEvent.raw_text,
    ).where(LogEvent.id.in_(list(ids)))
    return {row.id: row for row in (await session.execute(stmt)).all()}


async def level_counts(session: AsyncSession, where: list[ColumnElement[bool]], limit: int) -> list[Row]:
    """Events per source and level, with the first and last event time. Served by the source/time and level/time
    indexes; `limit` bounds the reply when a source's levels are messy (each odd level spelling is its own group)."""
    stmt = (
        select(
            LogEvent.source_id, LogEvent.level, func.count().label("count"),
            func.min(LogEvent.occurred_at).label("first_at"), func.max(LogEvent.occurred_at).label("last_at"),
        )
        .where(*where)
        .group_by(LogEvent.source_id, LogEvent.level)
        .order_by(LogEvent.source_id, desc("count"), LogEvent.level)
        .limit(limit)
    )
    return list((await session.execute(stmt)).all())
