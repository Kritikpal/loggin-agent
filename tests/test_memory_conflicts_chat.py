"""Open memory conflicts ride along on every /ai/chat response, so the chat can show them inline until they are
settled. They are read at the start of the turn: a conflict flagged in the background after a reply shows up on the
next message, in any conversation."""

import pytest
from langchain_core.messages import AIMessage

from app.repositories.memory import MemoryRepository
from tests.test_memory_api import GONE, MONSOON, mem, seed

pytestmark = pytest.mark.usefixtures("tool_session", "checkpointer")

URL = "/api/v1/ai/chat"
QUESTION = "How are jeans doing?"  # no change word: the router is skipped, so the agent is the only model call


async def ask(client, use_model) -> dict:
    use_model(AIMessage("Jeans are steady."))
    res = await client.post(URL, json={"message": QUESTION})
    assert res.status_code == 200
    return res.json()


async def test_an_open_conflict_comes_with_the_earlier_note(client, use_model, session):
    old = mem(MONSOON, at=0)
    flag = mem(GONE, status="conflicted", at=1, conflicts_with_id=old.id)
    flag.evidence = "Monsoon doesn't affect jeans"
    await seed(session, old, flag)

    [conflict] = (await ask(client, use_model))["memory_conflicts"]

    assert conflict["id"] == str(flag.id) and conflict["content"] == GONE
    assert conflict["evidence"] == "Monsoon doesn't affect jeans"
    assert (conflict["earlier_id"], conflict["earlier_content"]) == (str(old.id), MONSOON)
    assert set(conflict) == {"id", "content", "evidence", "earlier_id", "earlier_content", "created_at"}


async def test_no_conflicts_means_an_empty_list(client, use_model, session):
    old = mem(MONSOON, at=0)
    await seed(session, old, mem(GONE, status="superseded", at=1, conflicts_with_id=old.id),
               mem("Shirts sell out before Holi.", status="deleted", at=2, conflicts_with_id=old.id))

    assert (await ask(client, use_model))["memory_conflicts"] == []


async def test_at_most_three_conflicts_newest_first(client, use_model, session):
    old = mem(MONSOON, at=0)
    flags = [mem(f"Correction {word} about jeans.", status="conflicted", at=i + 1, conflicts_with_id=old.id)
             for i, word in enumerate(("one", "two", "three", "four"))]
    await seed(session, old, *flags)

    conflicts = (await ask(client, use_model))["memory_conflicts"]

    assert [c["content"] for c in conflicts] == [f.content for f in reversed(flags[1:])]


async def test_a_failed_conflict_lookup_never_fails_the_chat(client, use_model, monkeypatch):
    async def broken(self, limit):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(MemoryRepository, "open_conflicts", broken)

    body = await ask(client, use_model)

    assert body["memory_conflicts"] == [] and body["response"] == "Jeans are steady."
