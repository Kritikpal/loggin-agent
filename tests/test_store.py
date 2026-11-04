"""5.7: the event upsert and the per-source index state, against the real test database."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.core.embeddings import EMBEDDING_DIMENSIONS
from app.ingestion.readers.base import Cursor
from app.ingestion.store import EventRow, advance_state, insert_new_events, load_cursor, record_error, upsert_events
from app.models import LogEvent, SourceIndexState

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 29, 8, 0, tzinfo=UTC)
VECTOR = [0.25] * EMBEDDING_DIMENSIONS
CURSOR = Cursor(offset=10, line=3, head_len=10, head_sha="abc", file_id="1:2")


def row(n: int, text_: str = "ERROR pool timeout", embedding: list[float] | None = None, **overrides) -> EventRow:
    values = dict(
        event_key=f"{n}:k", locator={"type": "file", "offset_start": n}, occurred_at=T0 + timedelta(seconds=n),
        level="ERROR", raw_text=text_, fields={"pid": "1"}, semantic_text=text_, embedding=embedding,
        embedding_model="m" if embedding else None,
    )
    return EventRow(**{**values, **overrides})


async def write(factory, source_id: str, rows: list[EventRow]) -> None:
    async with factory() as s, s.begin():
        await upsert_events(s, source_id, rows)


async def count(factory, source_id: str | None = None) -> int:
    async with factory() as s:
        query = select(func.count()).select_from(LogEvent)
        if source_id:
            query = query.where(LogEvent.source_id == source_id)
        return await s.scalar(query)


async def one(factory, source_id: str = "a", key: str = "1:k") -> LogEvent:
    async with factory() as s:
        return (await s.execute(select(LogEvent).where(LogEvent.source_id == source_id, LogEvent.event_key == key))).scalar_one()


async def state(factory, source_id: str = "a") -> SourceIndexState | None:
    async with factory() as s:
        return await s.get(SourceIndexState, source_id)


async def test_re_upserting_the_same_rows_keeps_one_row_each(session_factory):
    rows = [row(n) for n in range(5)]
    await write(session_factory, "a", rows)
    await write(session_factory, "a", rows)

    assert await count(session_factory) == 5


async def test_empty_rows_are_a_no_op(session_factory):
    await write(session_factory, "a", [])

    assert await count(session_factory) == 0


async def test_a_changed_event_under_the_same_key_updates_the_row(session_factory):
    await write(session_factory, "a", [row(1, "old text")])
    await write(session_factory, "a", [row(1, "new text", level="WARN", fields={"pid": "2"}, locator={"type": "file", "offset_start": 9})])

    event = await one(session_factory)
    assert (event.raw_text, event.semantic_text, event.level, event.fields, event.locator["offset_start"]) == (
        "new text", "new text", "WARN", {"pid": "2"}, 9)


async def test_a_null_embedding_is_filled_later_and_never_cleared(session_factory):
    await write(session_factory, "a", [row(1)])
    assert (await one(session_factory)).embedding is None

    await write(session_factory, "a", [row(1, embedding=VECTOR)])
    filled = await one(session_factory)
    await write(session_factory, "a", [row(1, "changed")])  # a failed re-embed brings no vector
    kept = await one(session_factory)

    assert list(filled.embedding) == pytest.approx(VECTOR) and filled.embedding_model == "m"
    assert list(kept.embedding) == pytest.approx(VECTOR) and kept.embedding_model == "m" and kept.raw_text == "changed"


async def test_the_same_event_key_in_two_sources_coexists(session_factory):
    await write(session_factory, "a", [row(1)])
    await write(session_factory, "b", [row(1)])

    assert (await count(session_factory, "a"), await count(session_factory, "b")) == (1, 1)


async def test_the_keyword_index_is_generated_from_the_semantic_text(session_factory):
    await write(session_factory, "a", [row(1, "HikariPool timeout trace-abc123")])

    async with session_factory() as s:
        hits = await s.scalar(text("select count(*) from log_events where search_tsv @@ plainto_tsquery('simple', 'trace-abc123')"))
    assert hits == 1


async def test_events_and_cursor_commit_together(session_factory):
    async with session_factory() as s, s.begin():
        await upsert_events(s, "a", [row(1), row(2)])
        await advance_state(s, "a", CURSOR, T0, None)

    assert await count(session_factory) == 2
    async with session_factory() as s:
        assert await load_cursor(s, "a") == CURSOR


async def test_a_failure_inside_the_transaction_leaves_neither_rows_nor_cursor(session_factory):
    with pytest.raises(RuntimeError):
        async with session_factory() as s, s.begin():
            await upsert_events(s, "a", [row(1), row(2)])
            await advance_state(s, "a", CURSOR, T0, None)
            raise RuntimeError("crash after the upsert")

    assert await count(session_factory) == 0 and await state(session_factory) is None


async def advance(factory, indexed_through, last_error=None, cursor=CURSOR) -> SourceIndexState:
    async with factory() as s, s.begin():
        await advance_state(s, "a", cursor, indexed_through, last_error)
    return await state(factory)


async def test_indexed_through_only_moves_forward_and_survives_none(session_factory):
    first = await advance(session_factory, T0 + timedelta(hours=1))
    older = await advance(session_factory, T0)
    none = await advance(session_factory, None)

    assert first.indexed_through == older.indexed_through == none.indexed_through == T0 + timedelta(hours=1)


async def test_a_first_state_write_may_have_no_time(session_factory):
    assert (await advance(session_factory, None)).indexed_through is None


async def test_every_state_write_sets_updated_at(session_factory):
    first = await advance(session_factory, T0)
    second = await advance(session_factory, T0)
    async with session_factory() as s, s.begin():
        await record_error(s, "a", "OSError")
    third = await state(session_factory)

    assert first.updated_at < second.updated_at < third.updated_at


async def test_last_error_is_set_and_cleared(session_factory):
    assert (await advance(session_factory, T0, "OSError")).last_error == "OSError"
    assert (await advance(session_factory, T0, None)).last_error is None


async def test_record_error_leaves_cursor_and_time_alone(session_factory):
    await advance(session_factory, T0)
    async with session_factory() as s, s.begin():
        await record_error(s, "a", "FileNotFoundError")
    after = await state(session_factory)

    assert (after.last_error, after.indexed_through, Cursor.from_json(after.cursor)) == ("FileNotFoundError", T0, CURSOR)


async def test_record_error_creates_the_state_row_if_needed(session_factory):
    async with session_factory() as s, s.begin():
        await record_error(s, "a", "OSError")
    created = await state(session_factory)

    assert (created.last_error, created.cursor, created.indexed_through) == ("OSError", None, None)


async def test_load_cursor_defaults_and_round_trips(session_factory):
    async with session_factory() as s:
        assert await load_cursor(s, "a") == Cursor()
    await advance(session_factory, None)
    async with session_factory() as s:
        assert await load_cursor(s, "a") == CURSOR


async def test_a_damaged_stored_cursor_means_start_over(session_factory):
    async with session_factory() as s, s.begin():
        s.add(SourceIndexState(source_id="a", cursor="{not json"))
    async with session_factory() as s:
        assert await load_cursor(s, "a") == Cursor()


async def insert_new(factory, source_id: str, rows: list[EventRow]) -> int:
    async with factory() as s, s.begin():
        return await insert_new_events(s, source_id, rows)


async def test_insert_new_events_skips_existing_keys(session_factory):
    assert await insert_new(session_factory, "a", [row(1)]) == 1
    assert await insert_new(session_factory, "a", [row(1), row(2)]) == 1
    assert await count(session_factory, "a") == 2


async def test_insert_new_events_does_not_overwrite(session_factory):
    await insert_new(session_factory, "a", [row(1, "first")])
    await insert_new(session_factory, "a", [row(1, "second")])
    async with session_factory() as s:
        assert await s.scalar(select(LogEvent.raw_text)) == "first"


async def test_duplicates_inside_one_call_count_once(session_factory):
    assert await insert_new(session_factory, "a", [row(3), row(3)]) == 1
    assert await count(session_factory) == 1


async def test_concurrent_identical_inserts_leave_one_row(session_factory):
    inserted = await asyncio.gather(*(insert_new(session_factory, "a", [row(4)]) for _ in range(2)))
    assert sorted(inserted) == [0, 1]
    assert await count(session_factory) == 1


async def test_insert_new_events_with_no_rows_returns_zero(session_factory):
    assert await insert_new(session_factory, "a", []) == 0
