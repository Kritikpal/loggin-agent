"""Writes indexed events and each source's resume point to PostgreSQL.

Atomicity contract: the caller writes a batch's events AND its cursor advance inside ONE transaction

    async with session_factory() as session, session.begin():
        await upsert_events(session, source_id, rows)
        await advance_state(session, source_id, next_cursor, indexed_through, None)

so a crash can never leave the cursor ahead of the data (at worst the batch is read and upserted again). These
functions never commit themselves.

No compare-and-swap: run one indexer per source at a time. Upserts are idempotent, so a duplicate run is harmless,
but the cursor is last-writer-wins.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.readers.base import Cursor
from app.models.log_event import LogEvent, SourceIndexState

# Overwritten from the new row on conflict. search_tsv is GENERATED and never written; the vector has its own rule.
_REPLACED = ("locator", "occurred_at", "level", "raw_text", "fields", "semantic_text", "signature", "signature_template")


@dataclass(frozen=True)
class EventRow:
    """One redacted event ready to store. Every text in it must already be redacted."""

    event_key: str
    locator: Mapping[str, Any]
    occurred_at: datetime | None
    level: str | None
    raw_text: str
    fields: Mapping[str, Any]
    semantic_text: str
    embedding: list[float] | None = None  # None = not (yet) embedded
    embedding_model: str | None = None
    signature: str | None = None  # the event's kind (app.ingestion.signature)
    signature_template: str | None = None


async def upsert_events(session: AsyncSession, source_id: str, rows: Sequence[EventRow]) -> None:
    if not rows:
        return
    stmt = insert(LogEvent)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[LogEvent.source_id, LogEvent.event_key],
        set_={
            **{name: excluded[name] for name in _REPLACED},
            # a failed re-embed brings NULL: keep the good vector rather than erase it
            "embedding": func.coalesce(excluded.embedding, LogEvent.embedding),
            "embedding_model": func.coalesce(excluded.embedding_model, LogEvent.embedding_model),
        },
    )
    # executemany keeps the bind-parameter count per statement fixed, however large the batch
    await session.execute(stmt, [_values(source_id, row) for row in rows])


async def insert_new_events(session: AsyncSession, source_id: str, rows: Sequence[EventRow]) -> int:
    """Insert only events whose key is new for the source; existing rows are left exactly as they are (no overwrite,
    no re-embed). Returns how many rows were really inserted, so the caller can count the rest as duplicates."""
    if not rows:
        return 0
    stmt = insert(LogEvent).on_conflict_do_nothing(index_elements=[LogEvent.source_id, LogEvent.event_key])
    result = await session.execute(stmt.returning(LogEvent.id), [_values(source_id, row) for row in rows])
    return len(result.all())


def _values(source_id: str, row: EventRow) -> dict[str, Any]:
    return {
        "source_id": source_id, "event_key": row.event_key, "locator": dict(row.locator),
        "occurred_at": row.occurred_at, "level": row.level, "raw_text": row.raw_text, "fields": dict(row.fields),
        "semantic_text": row.semantic_text, "embedding": row.embedding, "embedding_model": row.embedding_model,
        "signature": row.signature, "signature_template": row.signature_template,
    }


async def advance_state(
    session: AsyncSession, source_id: str, cursor: Cursor, indexed_through: datetime | None, last_error: str | None
) -> None:
    """Store the resume point. indexed_through never moves back (GREATEST ignores NULL, so None keeps the old one)."""
    stmt = insert(SourceIndexState).values(
        source_id=source_id, cursor=cursor.to_json(), indexed_through=indexed_through, last_error=last_error,
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[SourceIndexState.source_id],
        set_={
            "cursor": stmt.excluded.cursor,
            "indexed_through": func.greatest(SourceIndexState.indexed_through, stmt.excluded.indexed_through),
            "last_error": stmt.excluded.last_error,
            "updated_at": func.now(),  # the column has no onupdate
        },
    )
    await session.execute(stmt)


async def record_error(session: AsyncSession, source_id: str, error: str) -> None:
    """Note a failure (an exception class name, never log content) without moving the cursor or indexed_through."""
    stmt = insert(SourceIndexState).values(source_id=source_id, last_error=error, updated_at=func.now())
    stmt = stmt.on_conflict_do_update(
        index_elements=[SourceIndexState.source_id],
        set_={"last_error": stmt.excluded.last_error, "updated_at": func.now()},
    )
    await session.execute(stmt)


async def load_cursor(session: AsyncSession, source_id: str) -> Cursor:
    stored = await session.scalar(select(SourceIndexState.cursor).where(SourceIndexState.source_id == source_id))
    return Cursor.from_json(stored)
