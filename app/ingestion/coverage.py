"""How complete the index is for each source, so an empty search result is never mistaken for "no such logs" (AC7).

Read-only: one SELECT on source_index_state plus a stat() of each file. The status rules, first match wins:

    not_indexed  no source_index_state row
    error        the last run recorded an error (an exception class name)
    stale        the file changed (or vanished) after the last run
    partial      the file did not change, but its tail is not consumed yet (e.g. the newest event was held back)
    complete     otherwise
"""

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.readers.base import Cursor
from app.ingestion.sources import Source
from app.models.log_event import LogEvent, SourceIndexState
from app.schemas.search_logs import SourceCoverage

if TYPE_CHECKING:
    from app.ingestion.projects import Project


async def coverage_for(session: AsyncSession, source: Source) -> SourceCoverage:
    return (await coverage_for_all(session, (source,)))[0]


async def coverage_for_all(session: AsyncSession, sources: Sequence[Source]) -> list[SourceCoverage]:
    """One coverage per source, in the given order."""
    ids = [source.id for source in sources]
    rows = (await session.scalars(select(SourceIndexState).where(SourceIndexState.source_id.in_(ids)))).all()
    states = {row.source_id: row for row in rows}
    # stat() blocks on the disk (or a network share), so the whole batch runs in one worker thread.
    files = await asyncio.to_thread(lambda: [_stat(source) for source in sources])
    return [_status(source, states.get(source.id), file) for source, file in zip(sources, files)]


async def time_spans(session: AsyncSession, sources: Sequence[Source]) -> dict[str, tuple[datetime, datetime]]:
    """First and last event time per source. A source with no timestamped events is left out."""
    first, last = func.min(LogEvent.occurred_at), func.max(LogEvent.occurred_at)
    rows = await session.execute(
        select(LogEvent.source_id, first, last)
        .where(LogEvent.source_id.in_([source.id for source in sources]), LogEvent.occurred_at.is_not(None))
        .group_by(LogEvent.source_id))
    return {source_id: (start, end) for source_id, start, end in rows}


@dataclass(frozen=True)
class FileStats:
    event_count: int
    first_at: datetime | None
    last_at: datetime | None
    parsed_ratio: float | None  # share of events that got both a level and a time; a low one = wrong type or file


async def file_stats(session: AsyncSession, source_ids: Sequence[str]) -> dict[str, FileStats]:
    """Event counts per file id. An id without events is left out."""
    parsed = func.count().filter(LogEvent.level.is_not(None), LogEvent.occurred_at.is_not(None))
    rows = await session.execute(
        select(LogEvent.source_id, func.count(), parsed, func.min(LogEvent.occurred_at), func.max(LogEvent.occurred_at))
        .where(LogEvent.source_id.in_(list(source_ids)))
        .group_by(LogEvent.source_id))
    return {
        source_id: FileStats(event_count=count, first_at=first, last_at=last, parsed_ratio=good / count)
        for source_id, count, good, first, last in rows
    }


async def orphan_ids(session: AsyncSession, project: "Project") -> list[str]:
    """Ids indexed under this project whose file is gone. autoescape: '_' in a project id is not a LIKE wildcard."""
    known = [source.id for source in project.files]
    rows = await session.scalars(
        select(SourceIndexState.source_id)
        .where(SourceIndexState.source_id.startswith(f"{project.id}/", autoescape=True),
               SourceIndexState.source_id.not_in(known))
        .order_by(SourceIndexState.source_id))
    return list(rows)


def _stat(source: Source) -> os.stat_result | None:
    try:
        return source.path.stat()
    except OSError:
        return None


def _status(source: Source, state: SourceIndexState | None, file: os.stat_result | None) -> SourceCoverage:
    if state is None:
        return SourceCoverage(source_id=source.id, status="not_indexed", indexed_through=None)
    coverage = SourceCoverage(
        source_id=source.id, status="complete", indexed_through=state.indexed_through, last_error=state.last_error)
    if state.last_error is not None:
        return coverage.model_copy(update={"status": "error"})
    # A file that vanished before any run noticed counts as changed; the next run records the error.
    if file is None or datetime.fromtimestamp(file.st_mtime, UTC) > state.updated_at:
        return coverage.model_copy(update={"status": "stale"})
    if Cursor.from_json(state.cursor).offset < file.st_size:
        return coverage.model_copy(update={"status": "partial"})
    return coverage
