"""Indexes allowlisted log sources: read -> redact -> resolve -> semantic text -> embed -> store. No LLM anywhere.

Per batch: the reader's blocking file I/O and the CPU-bound embedder run in worker threads (asyncio.to_thread) so the
event loop is never blocked; then the batch's rows and the cursor advance are written in ONE transaction (see
app.ingestion.store), so the cursor never runs ahead of the data.

Safety: redaction runs first and everything stored or embedded derives from the redacted text. Errors are recorded
and logged by exception CLASS NAME only, never with messages or tracebacks, because those can carry log content.

Failure policy: index_source never raises an Exception. An unexpected failure (database down, a bug) is recorded
best-effort in source_index_state.last_error, leaves the cursor where the last committed batch put it, and comes back
as a report with status "error", so callers (index_all, the CLI) carry on with the next source.

One indexer per source at a time (no in-process lock): see the store module for why a duplicate run is harmless.
"""

import asyncio
import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, embed_documents
from app.ingestion.fields.resolver import resolve
from app.ingestion.readers.base import ReadResult, SourceEvent
from app.ingestion.readers.file_reader import FileReader
from app.ingestion.redaction import redact
from app.ingestion.semantic_text import MAX_TEXT_CHARS, build_semantic_text
from app.ingestion.sources import FileSource, Source
from app.ingestion.signature import event_signature
from app.ingestion.store import EventRow, advance_state, load_cursor, record_error, upsert_events
from app.models.log_event import LogEvent

logger = logging.getLogger(__name__)

IndexStatus = Literal["ok", "rotated", "missing", "error"]
Embedder = Callable[[list[str]], list[list[float]]]
SessionFactory = Callable[[], AsyncSession]
DEFAULT_MAX_EVENTS = 200  # at ~12 embeddings/s one batch is ~17 s of work: short transactions, little lost on Ctrl-C
DEFAULT_EMBED_BATCH = 64


@dataclass(frozen=True)
class IndexReport:
    source_id: str
    events_read: int = 0
    events_stored: int = 0
    embedded: int = 0
    embed_failed: int = 0  # stored without a vector; reembed_missing fills them later
    batches: int = 0
    status: IndexStatus = "ok"  # rotated: at least one batch restarted a replaced or truncated file
    error: str | None = None  # an exception class name, never log content


async def index_source(
    source: Source,
    session_factory: SessionFactory,
    *,
    embed: Embedder = embed_documents,
    settle_seconds: float = 5.0,
    max_events: int = DEFAULT_MAX_EVENTS,
    embed_batch_size: int = DEFAULT_EMBED_BATCH,
) -> IndexReport:
    report = IndexReport(source.id)
    try:
        reader = FileReader(source, settle_seconds=settle_seconds)
        async with session_factory() as session:
            cursor = await load_cursor(session, source.id)
        while True:
            result = await asyncio.to_thread(reader.read_batch, cursor, max_events)
            if result.status in ("missing", "error"):
                error = result.error or result.status
                await _record_error(session_factory, source.id, error)
                return replace(report, status=result.status, error=error)
            report = await _store_batch(source, session_factory, result, report, embed, embed_batch_size)
            cursor = result.next_cursor
            if not result.has_more:
                return report
    except Exception as exc:
        error = type(exc).__name__
        logger.warning("Indexing source %s failed: %s", source.id, error)
        await _record_error_best_effort(session_factory, source.id, error)
        return replace(report, status="error", error=error)


async def index_all(sources: Sequence[Source], session_factory: SessionFactory, **kwargs) -> list[IndexReport]:
    """Sources one after another; index_source never raises, so one failing source cannot stop the rest."""
    return [await index_source(source, session_factory, **kwargs) for source in sources]


async def reembed_missing(
    session_factory: SessionFactory, source_id: str | None = None, *, embed: Embedder = embed_documents,
    chunk: int = DEFAULT_EMBED_BATCH,
) -> int:
    """Fill rows stored without a vector, `chunk` at a time. Stops at the first failing chunk, so a persistent
    failure cannot loop forever. Returns how many rows it filled."""
    filled = 0
    query = select(LogEvent.id, LogEvent.semantic_text).where(LogEvent.embedding.is_(None))
    if source_id is not None:
        query = query.where(LogEvent.source_id == source_id)
    while True:
        async with session_factory() as session:
            pending = (await session.execute(query.order_by(LogEvent.id).limit(max(1, chunk)))).all()
        if not pending:
            return filled
        vectors = await _embed_chunk(embed, [row.semantic_text for row in pending])
        if vectors is None:
            return filled
        updates = [{"id": row.id, "embedding": v, "embedding_model": EMBEDDING_MODEL} for row, v in zip(pending, vectors)]
        async with session_factory() as session, session.begin():
            await session.execute(update(LogEvent), updates)  # filled rows leave the IS NULL set: always progress
        filled += len(pending)


async def _store_batch(
    source: FileSource, session_factory: SessionFactory, result: ReadResult, report: IndexReport,
    embed: Embedder, embed_batch_size: int,
) -> IndexReport:
    rows = [_prepare(event, source) for event in result.events]
    rows, embedded = await _embed_rows(rows, embed, embed_batch_size)
    times = [row.occurred_at for row in rows if row.occurred_at is not None]
    async with session_factory() as session, session.begin():
        await upsert_events(session, source.id, rows)
        await advance_state(session, source.id, result.next_cursor, max(times, default=None), None)
    return replace(
        report,
        events_read=report.events_read + len(result.events),
        events_stored=report.events_stored + len(rows),
        embedded=report.embedded + embedded,
        embed_failed=report.embed_failed + len(rows) - embedded,
        batches=report.batches + 1,
        status="rotated" if result.status == "rotated" else report.status,
    )


def _prepare(event: SourceEvent, source: FileSource) -> EventRow:
    # Redaction first, and outside the guard: if it fails nothing may be stored, so the whole run fails instead.
    text = redact(event.event.text).text
    unparsed = event_signature(level=None, logger=None, message=None, raw_text=text)
    base = EventRow(event.event_key, dict(event.locator), None, None, text, {}, text[:MAX_TEXT_CHARS],
                    signature=unparsed.sig, signature_template=unparsed.template)
    try:
        resolved = resolve(text, source)
        semantic = build_semantic_text(resolved.parsed, source.app_packages).text
        parsed = resolved.parsed
        kind = event_signature(level=parsed.level, logger=parsed.logger, message=parsed.message, raw_text=text)
    except Exception as exc:  # resolve never raises by contract; never drop the event if it does
        logger.warning("Storing an event of source %s unparsed: %s", source.id, type(exc).__name__)
        return base
    return replace(
        base, occurred_at=resolved.occurred_at, level=parsed.level, fields=dict(resolved.fields),
        semantic_text=semantic, signature=kind.sig, signature_template=kind.template,
    )


async def _embed_rows(rows: list[EventRow], embed: Embedder, batch_size: int) -> tuple[list[EventRow], int]:
    out: list[EventRow] = []
    embedded = 0
    step = max(1, batch_size)
    for start in range(0, len(rows), step):
        chunk = rows[start:start + step]
        vectors = await _embed_chunk(embed, [row.semantic_text for row in chunk])
        if vectors is None:
            out += chunk  # stored without vectors; the cursor still advances
            continue
        out += [replace(row, embedding=v, embedding_model=EMBEDDING_MODEL) for row, v in zip(chunk, vectors)]
        embedded += len(chunk)
    return out, embedded


async def _embed_chunk(embed: Embedder, texts: list[str]) -> list[list[float]] | None:
    """The vectors, or None when the embedder fails or returns anything the vector column would reject."""
    try:
        vectors = [[float(x) for x in v] for v in await asyncio.to_thread(embed, texts)]
    except Exception as exc:
        logger.warning("Embedding %d texts failed: %s", len(texts), type(exc).__name__)
        return None
    valid = len(vectors) == len(texts) and all(
        len(v) == EMBEDDING_DIMENSIONS and all(math.isfinite(x) for x in v) for v in vectors
    )
    if not valid:
        logger.warning("Embedding %d texts returned unusable vectors", len(texts))
        return None
    return vectors


async def _record_error(session_factory: SessionFactory, source_id: str, error: str) -> None:
    async with session_factory() as session, session.begin():
        await record_error(session, source_id, error)


async def _record_error_best_effort(session_factory: SessionFactory, source_id: str, error: str) -> None:
    try:
        await _record_error(session_factory, source_id, error)
    except Exception as exc:
        logger.warning("Could not record the error of source %s: %s", source_id, type(exc).__name__)
