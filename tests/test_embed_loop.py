"""The background loop that gives pushed (vector-less) events their embeddings."""

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.embeddings import EMBEDDING_DIMENSIONS
from app.ingestion import embed_loop
from app.ingestion.embed_loop import run_embed_loop
from app.ingestion.store import EventRow, insert_new_events
from app.models import LogEvent

pytestmark = pytest.mark.integration

INTERVAL = 0.01


def fake_embed(texts: list[str]) -> list[list[float]]:
    return [[0.1] * EMBEDDING_DIMENSIONS for _ in texts]


def event(key: str) -> EventRow:
    return EventRow(
        event_key=key, locator={"kind": "push"}, occurred_at=datetime(2026, 11, 4, tzinfo=UTC), level="ERROR",
        raw_text="ERROR boom", fields={}, semantic_text="ERROR boom")


async def store(factory, *keys: str) -> None:
    async with factory() as s, s.begin():
        await insert_new_events(s, "demo/push", [event(k) for k in keys])


async def missing_vectors(factory) -> int:
    async with factory() as s:
        return len((await s.scalars(select(LogEvent.id).where(LogEvent.embedding.is_(None)))).all())


async def until(condition, timeout: float = 3.0) -> None:
    async def poll():
        while not await condition():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout)


@contextlib.asynccontextmanager
async def running(factory, embed=fake_embed):
    task = asyncio.create_task(run_embed_loop(factory, interval=INTERVAL, embed=embed))
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_loop_fills_missing_vectors(session_factory):
    await store(session_factory, "a", "b")
    async with running(session_factory):
        await until(lambda: _zero(session_factory))
    assert await missing_vectors(session_factory) == 0


async def _zero(factory) -> bool:
    return await missing_vectors(factory) == 0


async def test_loop_picks_up_rows_pushed_later(session_factory):
    async with running(session_factory):
        await asyncio.sleep(0.05)
        await store(session_factory, "late")
        await until(lambda: _zero(session_factory))


async def test_loop_survives_a_failing_pass(session_factory, monkeypatch):
    calls = []
    real = embed_loop.reembed_missing

    async def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database restarting")
        return await real(*args, **kwargs)

    monkeypatch.setattr(embed_loop, "reembed_missing", flaky)
    await store(session_factory, "a")
    async with running(session_factory):
        await until(lambda: _zero(session_factory))
    assert len(calls) >= 2


async def test_loop_stops_on_cancel(session_factory):
    task = asyncio.create_task(run_embed_loop(session_factory, interval=INTERVAL, embed=fake_embed))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


async def test_app_lifespan_starts_and_stops_the_loop(monkeypatch):
    from app import main

    started, stopped = asyncio.Event(), asyncio.Event()

    async def fake_loop(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stopped.set()
            raise

    monkeypatch.setattr(main, "run_embed_loop", fake_loop)
    async with main.lifespan(main.app):
        await asyncio.wait_for(started.wait(), 1)
        assert not stopped.is_set()
    assert stopped.is_set()
