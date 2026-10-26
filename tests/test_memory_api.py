"""GET/DELETE/POST accept on /api/v1/ai/memories: inspect, forget and settle a flagged conflict (spec 5.5)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.ai.memory import content_hash, recall
from app.models import Memory
from app.repositories.memory import MemoryRepository

URL = "/api/v1/ai/memories"
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
MONSOON = "Jeans sales slow down in monsoon, June to September."
GONE = "Monsoon no longer affects jeans."


def mem(content: str, *, kind="observation", status="active", at=0, **kw) -> Memory:
    fields = dict(subject="Jeans") if kind == "observation" else dict(key="report_week")
    return Memory(
        id=uuid.uuid4(), kind=kind, content=content, evidence="said", status=status, content_hash=content_hash(content),
        attributes={}, created_at=START + timedelta(seconds=at), **fields, **kw,
    )


async def seed(session, *memories: Memory) -> None:
    MemoryRepository(session).add(*memories)
    await session.commit()


async def status_of(session_factory, memory_id) -> str:
    async with session_factory() as s:
        return await s.scalar(select(Memory.status).where(Memory.id == memory_id))


async def test_list_shows_active_and_conflicted_newest_first_with_every_field(client, session):
    old = mem(MONSOON, at=0)
    flagged = mem(GONE, status="conflicted", at=1, conflicts_with_id=old.id)
    await seed(session, old, flagged, mem("Old note about shirts.", status="superseded", at=2),
               mem("Deleted note about shoes.", status="deleted", at=3))

    res = await client.get(URL)

    body = res.json()
    assert res.status_code == 200 and [m["content"] for m in body] == [GONE, MONSOON]
    assert body[0]["status"] == "conflicted" and body[0]["conflicts_with_id"] == str(old.id)
    assert set(body[0]) == {
        "id", "kind", "key", "subject", "content", "attributes", "evidence", "status", "replaces_id",
        "conflicts_with_id", "source_conversation_id", "created_at",
    }


async def test_list_filters_by_kind_and_status(client, session):
    await seed(session, mem(MONSOON), mem("Wants weekly reports.", kind="preference"),
               mem("Deleted note about shoes.", status="deleted"))

    kinds = [m["kind"] for m in (await client.get(URL, params={"kind": "preference"})).json()]
    deleted = [m["content"] for m in (await client.get(URL, params={"status": "deleted"})).json()]

    assert kinds == ["preference"] and deleted == ["Deleted note about shoes."]
    assert (await client.get(URL, params={"kind": "episode"})).status_code == 422
    assert (await client.get(URL, params={"status": "archived"})).status_code == 422


async def test_delete_forgets_a_memory_and_recall_stops_using_it(client, session, session_factory):
    note = mem(MONSOON)
    await seed(session, note)
    assert "[Jeans]" in await recall(session, "How are jeans doing?", [])

    res = await client.delete(f"{URL}/{note.id}")

    assert res.status_code == 204 and res.content == b""
    assert await status_of(session_factory, note.id) == "deleted"
    assert (await client.get(URL)).json() == []
    assert await recall(session, "How are jeans doing?", []) == ""


async def test_deleting_a_conflicted_row_rejects_it_and_keeps_the_old_one(client, session, session_factory):
    old = mem(MONSOON)
    flagged = mem(GONE, status="conflicted", at=1, conflicts_with_id=old.id)
    await seed(session, old, flagged)

    assert (await client.delete(f"{URL}/{flagged.id}")).status_code == 204

    assert await status_of(session_factory, flagged.id) == "deleted"
    assert await status_of(session_factory, old.id) == "active"


async def test_unknown_ids_are_404(client):
    missing = uuid.uuid4()

    assert (await client.delete(f"{URL}/{missing}")).status_code == 404
    assert (await client.post(f"{URL}/{missing}/accept")).status_code == 404
    assert (await client.delete(f"{URL}/not-a-uuid")).status_code == 422


async def test_accept_swaps_the_flagged_row_in_for_the_old_one(client, session, session_factory):
    old = mem(MONSOON)
    flagged = mem(GONE, status="conflicted", at=1, conflicts_with_id=old.id)
    await seed(session, old, flagged)

    res = await client.post(f"{URL}/{flagged.id}/accept")

    assert res.status_code == 200 and res.json()["status"] == "active" and res.json()["content"] == GONE
    assert await status_of(session_factory, old.id) == "superseded"
    notes = await recall(session, "How are jeans doing?", [])
    assert GONE in notes and "slow down in monsoon" not in notes


async def test_accept_leaves_a_since_deleted_old_row_alone(client, session, session_factory):
    old = mem(MONSOON, status="deleted")
    flagged = mem(GONE, status="conflicted", at=1, conflicts_with_id=old.id)
    await seed(session, old, flagged)

    assert (await client.post(f"{URL}/{flagged.id}/accept")).status_code == 200

    assert await status_of(session_factory, old.id) == "deleted"
    assert await status_of(session_factory, flagged.id) == "active"


async def test_accept_only_works_on_conflicted_rows(client, session):
    active, gone = mem(MONSOON), mem("Deleted note about shoes.", status="deleted")
    await seed(session, active, gone)

    for row in (active, gone):
        res = await client.post(f"{URL}/{row.id}/accept")
        assert res.status_code == 409 and res.json()["error"]["code"] == "conflict"
