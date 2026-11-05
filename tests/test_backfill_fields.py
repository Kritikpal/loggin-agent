"""Backfill: stored events get the fields the current parser finds in their text, without touching anything else."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.embeddings import EMBEDDING_DIMENSIONS
from app.ingestion.sources import PushSource
from app.ingestion.store import EventRow, upsert_events
from app.models.log_event import LogEvent
from scripts.backfill_fields import backfill, chosen_sources
from tests.project_helpers import make_project

pytestmark = pytest.mark.integration

PUSH = PushSource(id="demo/push", token_env="DEMO_TOKEN")
AUDIT = (
    "TID: [-1234] [] [2026-11-05 16:03:26,686] [f39cb36f]  INFO {AUDIT_LOG} - Initiator : Goutam003 | Action : Login | "
    'Target : ApplicationAuthenticationFramework | Data : { "ServiceProviderName" : "portalApplication" } | '
    "Result : Failed"
)


def event(key: str, raw_text: str = AUDIT, **overrides) -> EventRow:
    values = dict(
        event_key=key, locator={"kind": "push"}, occurred_at=datetime(2026, 11, 5, tzinfo=UTC), level="INFO",
        raw_text=raw_text, fields={}, semantic_text="semantic " + key, embedding=[0.5] * EMBEDDING_DIMENSIONS,
        embedding_model="m", signature="sig-" + key, signature_template="tpl " + key)
    return EventRow(**{**values, **overrides})


async def store(factory, source_id: str, *rows: EventRow) -> None:
    async with factory() as session, session.begin():
        await upsert_events(session, source_id, rows)


async def stored(factory, source_id: str = "demo/push") -> list[LogEvent]:
    async with factory() as session:
        found = await session.scalars(select(LogEvent).where(LogEvent.source_id == source_id).order_by(LogEvent.event_key))
        return list(found)


async def test_backfill_fills_audit_fields_of_stored_rows(session_factory):
    await store(session_factory, "demo/push", event("a"), event("b", raw_text="plain text"))
    assert await backfill(session_factory, [PUSH]) == 1
    audit, plain = await stored(session_factory)
    assert audit.fields["audit_action"] == "Login" and audit.fields["audit_application"] == "portalApplication"
    assert audit.fields["audit_result"] == "Failed" and audit.fields["audit_actor"] == "Goutam003"
    assert plain.fields == {}


async def test_a_second_run_changes_nothing(session_factory):
    await store(session_factory, "demo/push", event("a"))
    await backfill(session_factory, [PUSH])
    assert await backfill(session_factory, [PUSH]) == 0


async def test_other_columns_are_untouched(session_factory):
    await store(session_factory, "demo/push", event("a"))
    (before,) = await stored(session_factory)
    await backfill(session_factory, [PUSH])
    (after,) = await stored(session_factory)
    for column in ("raw_text", "semantic_text", "signature", "signature_template", "occurred_at", "level",
                   "embedding_model", "locator", "event_key", "created_at"):
        assert getattr(after, column) == getattr(before, column), column
    assert after.embedding is not None and list(after.embedding) == list(before.embedding)


async def test_rows_of_other_sources_are_untouched(session_factory):
    await store(session_factory, "demo/push", event("a"))
    await store(session_factory, "other/push", event("a"))
    await backfill(session_factory, [PUSH], batch_size=1)
    (other,) = await stored(session_factory, "other/push")
    assert other.fields == {}


async def test_batches_cover_every_row(session_factory):
    await store(session_factory, "demo/push", *(event(f"k{i}") for i in range(5)))
    assert await backfill(session_factory, [PUSH], batch_size=2) == 5


def test_the_script_covers_the_push_source_as_well_as_files():
    project = make_project(push_source=PUSH)
    assert chosen_sources((project,), None) == [PUSH]
    assert chosen_sources((project,), "demo") == [PUSH]
    assert chosen_sources((make_project(error="bad", push_source=PUSH),), None) == []
