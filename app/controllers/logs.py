"""Read a project's logs back: a filtered, paged list, one event, a histogram and filter counts. Open: no keys.

No request may scan a project's whole log. Every query is bounded three ways:
  * a time window (default: the last 24 h of what is indexed; at most 31 days);
  * an index-friendly shape: `source_id = ?` plus `ORDER BY occurred_at, id`, served by ix_log_events_source_time_id;
  * a row cap (SCAN_CAP) for the aggregates, a page limit for the list, and a statement timeout for everything.
Events without a timestamp lie outside every window, so they are not listed.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, bindparam, false, func, literal_column, select, text, tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.controllers.dependencies import get_project
from app.core.database import get_session
from app.core.errors import InvalidPayloadError, NotFoundError, QueryTimeoutError
from app.ingestion.projects import NOT_FOUND, Project
from app.models import LogEvent
from app.schemas.logs import (
    LevelFacet, LogDetail, LogFacets, LogItem, LogPage, LogStats, SignatureFacet, StatsBucket,
)

router = APIRouter(prefix="/projects/{project_id}/logs", tags=["Logs"])

TEXT_LIMIT = 2000
MAX_BUCKETS = 1000
FACET_TOP = 10
UNKNOWN_LEVEL = "UNKNOWN"
DEFAULT_WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(days=31)
SCAN_CAP = 50_000  # most rows an aggregate (stats, facets) reads; past it the reply says `sampled`
STATEMENT_TIMEOUT_MS = 5000
INTERVALS = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "1h": timedelta(hours=1), "1d": timedelta(days=1)}
SQL_INTERVALS = {"1m": "1 minute", "5m": "5 minutes", "1h": "1 hour", "1d": "1 day"}  # fixed strings: never user input
Interval = Literal["1m", "5m", "1h", "1d"]
TIMED_OUT = "statement timeout"


@dataclass(frozen=True)
class Filters:
    project: Project
    start: datetime
    end: datetime  # exclusive
    level: str | None
    q: str | None
    signature: str | None

    def clauses(self) -> list[ColumnElement[bool]]:
        ids = [source.id for source in self.project.sources]
        out = [LogEvent.source_id.in_(ids) if ids else false(), LogEvent.occurred_at >= self.start, LogEvent.occurred_at < self.end]
        if self.level:
            out.append(LogEvent.level == self.level.upper())  # levels are stored upper-case: this uses the index
        if self.q:
            # a case-insensitive substring, so "SocketTimeoutException" finds java.net.SocketTimeoutException. It only
            # filters rows already inside the window and the source index range.
            # ponytail: no trigram index; add pg_trgm on raw_text if windows get large enough to hit the timeout.
            out.append(LogEvent.raw_text.icontains(self.q, autoescape=True))
        if self.signature:
            out.append(LogEvent.signature == self.signature)
        return out


async def _run(session: AsyncSession, statement):
    """Execute, turning the statement timeout into a clear 'narrow it' error."""
    try:
        return await session.execute(statement)
    except DBAPIError as exc:
        if TIMED_OUT in str(exc.orig).lower():
            raise QueryTimeoutError("That query took too long. Narrow the time range or add a filter.") from None
        raise


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


async def get_filters(
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    start: Annotated[datetime | None, Query(alias="from", description="Events at or after this time (default: 24 h before `to`)")] = None,
    end: Annotated[datetime | None, Query(alias="to", description="Events before this time (default: just after the newest event)")] = None,
    level: Annotated[str | None, Query(max_length=20)] = None,
    q: Annotated[str | None, Query(max_length=200, description="Text to find in the log line (case-insensitive substring)")] = None,
    signature: Annotated[str | None, Query(max_length=200)] = None,
) -> Filters:
    await session.execute(text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))  # lasts for this request's transaction
    start, end = _aware(start), _aware(end)
    if end is None:
        ids = [source.id for source in project.sources]
        newest = await _run(session, select(LogEvent.occurred_at).where(
            LogEvent.source_id.in_(ids) if ids else false(), LogEvent.occurred_at.is_not(None))
            .order_by(LogEvent.occurred_at.desc()).limit(1))
        latest = newest.scalar_one_or_none()
        end = latest + timedelta(seconds=1) if latest else datetime.now(UTC)
    start = start or end - DEFAULT_WINDOW
    if end <= start:
        raise InvalidPayloadError("'to' must be after 'from'.")
    if end - start > MAX_WINDOW:
        raise InvalidPayloadError(f"The time range is at most {MAX_WINDOW.days} days; narrow 'from' and 'to'.")
    return Filters(project, start, end, level, q, signature)


FilterDep = Annotated[Filters, Depends(get_filters)]
Session = Annotated[AsyncSession, Depends(get_session)]


def _short(source_id: str) -> str:
    return source_id.split("/", 1)[1]


def _encode(ts: datetime, event_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{event_id}".encode()).decode()


def _decode(cursor: str) -> tuple[datetime, UUID]:
    try:
        stamp, _, ident = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
        return datetime.fromisoformat(stamp), UUID(ident)
    except (binascii.Error, ValueError):
        raise InvalidPayloadError("Invalid cursor.") from None


def list_statement(filters: Filters, *, ascending: bool, limit: int, cursor: str | None = None) -> Select:
    """One page: an index range scan over (source_id, occurred_at, id), read forward or backward, never sorted."""
    where = filters.clauses()
    key = tuple_(LogEvent.occurred_at, LogEvent.id)
    if cursor:
        ts, event_id = _decode(cursor)
        where.append(key > tuple_(ts, event_id) if ascending else key < tuple_(ts, event_id))
    order = (LogEvent.occurred_at.asc(), LogEvent.id.asc()) if ascending else (LogEvent.occurred_at.desc(), LogEvent.id.desc())
    return (
        select(LogEvent.id, LogEvent.source_id, LogEvent.occurred_at, LogEvent.level, LogEvent.signature,
               func.left(LogEvent.raw_text, TEXT_LIMIT), func.length(LogEvent.raw_text),
               LogEvent.fields["audit_actor"].as_string(), LogEvent.fields["audit_action"].as_string())
        .where(*where).order_by(*order).limit(limit + 1))


def _capped(filters: Filters, *columns):
    """The newest SCAN_CAP rows of the window (plus one, to know the cap was hit), flagged `over` for that extra one."""
    newest_first = (LogEvent.occurred_at.desc(), LogEvent.id.desc())
    position = func.row_number().over(order_by=newest_first)
    return (select(*columns, (position > SCAN_CAP).label("over"))
            .where(*filters.clauses()).order_by(*newest_first).limit(SCAN_CAP + 1).subquery())


def _auto_interval(span: timedelta) -> Interval:
    if span <= timedelta(hours=2):
        return "1m"
    if span <= timedelta(hours=12):
        return "5m"
    if span <= timedelta(days=5):
        return "1h"
    return "1d"


@router.get("", response_model=LogPage, summary="Events of a project in a time window, paged (keyset cursor)")
async def list_logs(
    filters: FilterDep, session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    sort: Literal["asc", "desc"] = "desc",
) -> LogPage:
    rows = (await _run(session, list_statement(filters, ascending=sort == "asc", limit=limit, cursor=cursor))).all()
    page, more = rows[:limit], len(rows) > limit
    items = [LogItem(id=r[0], source=_short(r[1]), occurred_at=r[2], level=r[3], signature=r[4], text=r[5],
                     truncated=r[6] > TEXT_LIMIT, actor=r[7], action=r[8]) for r in page]
    return LogPage(items=items, next_cursor=_encode(page[-1][2], page[-1][0]) if more else None)


@router.get("/stats", response_model=LogStats, summary="Events per time bucket and level, for a histogram")
async def log_stats(filters: FilterDep, session: Session, interval: Interval | None = None) -> LogStats:
    interval = interval or _auto_interval(filters.end - filters.start)
    if (filters.end - filters.start) / INTERVALS[interval] > MAX_BUCKETS:
        raise InvalidPayloadError(f"That range holds more than {MAX_BUCKETS} '{interval}' buckets; narrow it or use a larger interval.")
    inner = _capped(filters, LogEvent.occurred_at, func.coalesce(LogEvent.level, UNKNOWN_LEVEL).label("level"))
    bucket = func.date_bin(literal_column(f"interval '{SQL_INTERVALS[interval]}'"), inner.c.occurred_at,
                           bindparam("origin", filters.start)).label("bucket")
    rows = await _run(session, select(bucket, inner.c.level, func.count().filter(inner.c.over.is_(False)), func.bool_or(inner.c.over))
                      .group_by(bucket, inner.c.level).order_by(bucket))
    buckets: dict[datetime, dict[str, int]] = {}
    sampled = False
    for start_at, level, count, over in rows:
        sampled = sampled or bool(over)
        if count:
            buckets.setdefault(start_at, {})[level] = count
    return LogStats(interval=interval, sampled=sampled, buckets=[
        StatsBucket(start=s, total=sum(c.values()), by_level=c) for s, c in buckets.items()])


@router.get("/facets", response_model=LogFacets, summary="Most common levels and event kinds in the window")
async def log_facets(filters: FilterDep, session: Session) -> LogFacets:
    by_level = _capped(filters, func.coalesce(LogEvent.level, UNKNOWN_LEVEL).label("level"))
    levels = await _run(session, select(by_level.c.level, func.count().filter(by_level.c.over.is_(False)).label("n"), func.bool_or(by_level.c.over))
                        .group_by(by_level.c.level).order_by(literal_column("n").desc()))
    by_sig = _capped(filters, LogEvent.signature, LogEvent.signature_template)
    sigs = await _run(session, select(by_sig.c.signature, func.min(by_sig.c.signature_template), func.count().filter(by_sig.c.over.is_(False)).label("n"), func.bool_or(by_sig.c.over))
                      .where(by_sig.c.signature.is_not(None)).group_by(by_sig.c.signature)
                      .order_by(literal_column("n").desc(), by_sig.c.signature).limit(FACET_TOP))
    level_rows, sig_rows = levels.all(), sigs.all()
    return LogFacets(
        sampled=any(r[2] for r in level_rows),
        levels=[LevelFacet(value=v, count=n) for v, n, _ in level_rows if n],
        signatures=[SignatureFacet(value=v, template=t, count=n) for v, t, n, _ in sig_rows if n])


@router.get("/{event_id}", response_model=LogDetail, summary="One event, whole")
async def log_detail(event_id: UUID, project: Annotated[Project, Depends(get_project)], session: Session) -> LogDetail:
    event = await session.scalar(select(LogEvent).where(
        LogEvent.id == event_id, LogEvent.source_id.in_([s.id for s in project.sources]) if project.sources else false()))
    if event is None:
        raise NotFoundError(NOT_FOUND)
    return LogDetail(
        id=event.id, source=_short(event.source_id), locator=event.locator, occurred_at=event.occurred_at,
        level=event.level, raw_text=event.raw_text, fields=event.fields, signature=event.signature,
        signature_template=event.signature_template)
