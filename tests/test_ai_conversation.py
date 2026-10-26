"""Conversation memory on /api/v1/ai/chat: history reaches the model, turns are stored only when they succeed, and a
clear yes/no in chat decides the pending proposal. Scripted model, real tools, test database, real checkpointer."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, SystemMessage
from sqlalchemy import func, select

from app.ai.actions import campaign as campaign_action
from app.ai.graphs import operator_graph
from app.models import Campaign, ChatMessage
from app.repositories.chat_message import ChatMessageRepository
from tests.scripted_model import plan, route, verdict

pytestmark = pytest.mark.usefixtures("tool_session", "checkpointer")

URL = "/api/v1/ai/chat"
RESUME = "/api/v1/ai/workflows/{}/resume"


async def rows(session, conv) -> list[tuple]:
    session.expire_all()
    return [(m.role, m.content, m.thread_id) for m in await ChatMessageRepository(session).recent(uuid.UUID(str(conv)), 100)]


async def say(client, conv, text: str):
    return await client.post(URL, json={"message": text, "conversation_id": str(conv)})


def turns(model, call: int = 0) -> list[tuple[str, str]]:
    """(type, text) of what one model call saw, without the system prompt."""
    return [(type(m).__name__, m.text) for m in model.seen[call] if not isinstance(m, SystemMessage)]


async def campaign_count(session) -> int:
    return await session.scalar(select(func.count()).select_from(Campaign))


async def waiting(client, use_model, make_product, conv=None) -> dict:
    """A CREATE_CAMPAIGN turn that ends WAITING_APPROVAL (in `conv`, or a new conversation)."""
    await make_product(category="Jeans")
    check = [verdict("OTHER")] if conv else []  # with a proposal already pending, the approval check runs first
    use_model(*check, AIMessage("Jeans are slow."), route("ACTION"), plan())
    body = {"message": "Run a 20% promotion on jeans"} | ({"conversation_id": str(conv)} if conv else {})
    res = (await client.post(URL, json=body)).json()
    assert res["status"] == "WAITING_APPROVAL"
    return res


# ---- memory -----------------------------------------------------------------------------------------------------


async def test_new_conversation_stores_the_turn(client, use_model, session):
    use_model(AIMessage("Hello!"), route("ANSWER"))

    body = (await client.post(URL, json={"message": "Hi"})).json()

    assert await rows(session, body["conversation_id"]) == [("user", "Hi", None), ("assistant", "Hello!", None)]


async def test_follow_ups_see_earlier_turns(client, use_model, make_product):
    await make_product(category="Jeans")
    use_model(AIMessage("September 2026: $59,205."), route("ANSWER"))
    first = (await client.post(URL, json={"message": "Sales in September 2026?"})).json()
    conv = first["conversation_id"]

    second_model = use_model(AIMessage("Jeans in September 2026: $26,684."), route("ANSWER"))
    second = (await say(client, conv, "And for jeans only?")).json()

    third_model = use_model(AIMessage("Jeans lead."), route("ACTION"), plan(discount_percent=15))
    third = (await say(client, conv, "Run a 15% campaign on that category")).json()

    assert turns(second_model) == [
        ("HumanMessage", "Sales in September 2026?"), ("AIMessage", "September 2026: $59,205."),
        ("HumanMessage", "And for jeans only?"),
    ]
    planner_input = "\n".join(m.text for m in third_model.seen[2])
    assert "Conversation so far:" in planner_input and "Jeans in September 2026: $26,684." in planner_input
    assert third["status"] == "WAITING_APPROVAL" and third["proposed_action"]["discount_percent"] == "15.00"
    assert first["conversation_id"] == second["conversation_id"] == third["conversation_id"]


async def test_only_the_last_10_messages_reach_the_model(client, use_model, session):
    conv = uuid.uuid4()
    start = datetime(2026, 10, 1, tzinfo=UTC)
    session.add_all(
        ChatMessage(conversation_id=conv, role="user" if i % 2 == 0 else "assistant", content=f"m{i}",
                    created_at=start + timedelta(seconds=i))
        for i in range(12)
    )
    await session.commit()
    model = use_model(AIMessage("ok"), route("ANSWER"))

    await say(client, conv, "next")

    assert [text for _, text in turns(model)] == [f"m{i}" for i in range(2, 12)] + ["next"]


async def test_waiting_turn_stores_the_proposal_and_its_thread(client, use_model, make_product, session):
    body = await waiting(client, use_model, make_product)

    [_, (role, content, thread_id)] = await rows(session, body["conversation_id"])

    assert role == "assistant" and str(thread_id) == body["thread_id"]
    assert "Proposed campaign: Summer Jeans Sale | Jeans | 20.00% | message: 20% off all jeans this week." in content


async def test_correction_after_invalid_action_reaches_the_planner(client, use_model, make_product):
    await make_product(category="Jeans")
    use_model(AIMessage("Which category?"), route("ACTION"), plan(category=None))
    first = (await client.post(URL, json={"message": "Run a 20% promotion"})).json()
    assert first["status"] == "INVALID_ACTION"

    model = use_model(AIMessage("Jeans it is."), route("ACTION"), plan())
    second = (await say(client, first["conversation_id"], "The category is jeans")).json()

    assert "Missing: category" in "\n".join(m.text for m in model.seen[2])
    assert second["status"] == "WAITING_APPROVAL"


async def test_unknown_conversation_is_404_and_saves_nothing(client, use_model, session):
    use_model()
    conv = uuid.uuid4()

    res = await say(client, conv, "hello?")

    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"
    assert await rows(session, conv) == []


async def test_malformed_conversation_id_is_422(client, use_model):
    use_model()
    res = await say(client, "not-a-uuid", "hello?")
    assert res.status_code == 422


async def test_failed_turn_saves_nothing(client, use_model, session):
    use_model(AIMessage("Hello!"), route("ANSWER"))
    conv = (await client.post(URL, json={"message": "Hi"})).json()["conversation_id"]
    use_model()  # empty script: the first model call raises

    res = await say(client, conv, "Sales?")

    assert res.status_code == 503
    assert len(await rows(session, conv)) == 2


# ---- approving or rejecting from chat ---------------------------------------------------------------------------


async def test_yes_executes_the_pending_campaign(client, use_model, make_product, session):
    body = await waiting(client, use_model, make_product)
    model = use_model(verdict("APPROVE"))

    res = (await say(client, body["conversation_id"], "yes go ahead")).json()

    assert res["status"] == "EXECUTED" and res["campaign"]["id"] == body["thread_id"]
    assert await campaign_count(session) == 1
    assert len(model.seen) == 1  # only the approval check ran
    last = (await rows(session, body["conversation_id"]))[-1]
    assert f"Created campaign: Summer Jeans Sale (id {body['thread_id']})" in last[1]


async def test_no_rejects_it(client, use_model, make_product, session):
    body = await waiting(client, use_model, make_product)
    use_model(verdict("REJECT"))

    res = (await say(client, body["conversation_id"], "no, cancel it")).json()

    assert res["status"] == "REJECTED"
    assert await campaign_count(session) == 0


async def test_change_request_is_other_and_stays_pending(client, use_model, make_product, session):
    body = await waiting(client, use_model, make_product)
    use_model(verdict("OTHER"), AIMessage("I can prepare a new proposal at 10%."), route("ANSWER"))
    other = (await say(client, body["conversation_id"], "make it 10%")).json()
    use_model(verdict("APPROVE"))

    approved = (await say(client, body["conversation_id"], "ok, approve the original")).json()

    assert other["status"] == "COMPLETED"
    assert approved["status"] == "EXECUTED" and await campaign_count(session) == 1


async def test_unusable_verdict_is_other(client, use_model, make_product, session):
    body = await waiting(client, use_model, make_product)
    use_model(AIMessage("not structured"), AIMessage("Here is what I found."), route("ANSWER"))

    res = (await say(client, body["conversation_id"], "yes?")).json()

    assert res["status"] == "COMPLETED"
    assert await campaign_count(session) == 0


async def test_no_approval_check_without_a_pending_proposal(client, use_model):
    model = use_model(AIMessage("Yes to what?"), route("ANSWER"))

    res = (await client.post(URL, json={"message": "yes"})).json()

    assert res["status"] == "COMPLETED" and len(model.seen) == 2  # agent + router, no approval call


async def test_newest_proposal_is_the_one_approved(client, use_model, make_product, session):
    first = await waiting(client, use_model, make_product)
    second = await waiting(client, use_model, make_product, conv=first["conversation_id"])
    use_model(verdict("APPROVE"))

    res = (await say(client, first["conversation_id"], "yes")).json()

    assert res["campaign"]["id"] == second["thread_id"]
    older = await client.post(RESUME.format(first["thread_id"]), json={"decision": "APPROVED"})
    assert older.status_code == 200 and older.json()["status"] == "EXECUTED"  # still approvable with the button


async def test_missing_checkpoint_makes_yes_a_normal_message(client, use_model, make_product, checkpointer):
    body = await waiting(client, use_model, make_product)
    async with checkpointer.conn.connection() as conn:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            await conn.execute(f"DELETE FROM {table} WHERE thread_id = %s", (body["thread_id"],))
    use_model(AIMessage("There is nothing waiting for approval."), route("ANSWER"))

    res = await say(client, body["conversation_id"], "yes")

    assert res.status_code == 200 and res.json()["status"] == "COMPLETED"


async def test_failed_chat_approval_saves_nothing_and_can_be_retried(
    client, use_model, make_product, session, monkeypatch
):
    body = await waiting(client, use_model, make_product)
    stored = len(await rows(session, body["conversation_id"]))
    real = campaign_action.execute_proposal

    async def crash(proposal, campaign_id):
        raise RuntimeError("database went away")

    monkeypatch.setattr(campaign_action, "execute_proposal", crash)
    use_model(verdict("APPROVE"))
    assert (await say(client, body["conversation_id"], "yes")).status_code == 503
    assert len(await rows(session, body["conversation_id"])) == stored

    monkeypatch.setattr(campaign_action, "execute_proposal", real)
    use_model(verdict("APPROVE"))
    res = (await say(client, body["conversation_id"], "yes")).json()

    assert res["status"] == "EXECUTED" and await campaign_count(session) == 1


async def test_yes_after_no_is_a_normal_message(client, use_model, make_product, session):
    body = await waiting(client, use_model, make_product)
    use_model(verdict("REJECT"))
    await say(client, body["conversation_id"], "no")
    model = use_model(AIMessage("That proposal was rejected."), route("ANSWER"))

    res = (await say(client, body["conversation_id"], "actually yes")).json()

    assert res["status"] == "COMPLETED" and len(model.seen) == 2
    assert await campaign_count(session) == 0


# ---- the approval button --------------------------------------------------------------------------------------


async def test_button_approval_is_recorded_and_yes_is_then_normal(client, use_model, make_product, session):
    body = await waiting(client, use_model, make_product)

    res = (await client.post(RESUME.format(body["thread_id"]), json={"decision": "APPROVED"})).json()

    assert res["conversation_id"] == body["conversation_id"]
    last = (await rows(session, body["conversation_id"]))[-1]
    assert last == (
        "assistant",
        f"Approved with the approval button. Campaign 'Summer Jeans Sale' was created (id {body['thread_id']}).",
        uuid.UUID(body["thread_id"]),
    )
    model = use_model(AIMessage("It's already approved."), route("ANSWER"))
    assert (await say(client, body["conversation_id"], "yes")).json()["status"] == "COMPLETED"
    assert len(model.seen) == 2  # no approval check: nothing is pending any more


async def test_button_rejection_is_recorded(client, use_model, make_product, session):
    body = await waiting(client, use_model, make_product)

    await client.post(RESUME.format(body["thread_id"]), json={"decision": "REJECTED"})

    last = (await rows(session, body["conversation_id"]))[-1]
    assert last[:2] == ("assistant", "Rejected with the approval button. Nothing was changed.")


async def test_workflow_without_a_conversation_still_resumes(client, use_model, make_product, checkpointer, session):
    await make_product(category="Jeans")
    model = use_model(AIMessage("Jeans are slow."), route("ACTION"), plan())
    waiting_body = await operator_graph.run_operator(model, checkpointer, "Run a 20% promotion on jeans")

    res = await client.post(RESUME.format(waiting_body.thread_id), json={"decision": "APPROVED"})

    assert res.status_code == 200 and res.json()["conversation_id"] is None
    assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 0


async def test_button_on_the_older_proposal_leaves_yes_for_the_newer_one(client, use_model, make_product, session):
    first = await waiting(client, use_model, make_product)
    second = await waiting(client, use_model, make_product, conv=first["conversation_id"])
    await client.post(RESUME.format(first["thread_id"]), json={"decision": "APPROVED"})  # outcome row names A
    use_model(verdict("APPROVE"))

    res = (await say(client, first["conversation_id"], "yes")).json()

    assert res["status"] == "EXECUTED" and res["campaign"]["id"] == second["thread_id"]


def break_history_saves(monkeypatch) -> None:
    def fail(self, *messages):
        raise RuntimeError("chat_messages unavailable")

    monkeypatch.setattr(ChatMessageRepository, "add", fail)


async def test_a_real_decision_is_reported_even_if_saving_the_turn_fails(
    client, use_model, make_product, session, monkeypatch
):
    body = await waiting(client, use_model, make_product)
    break_history_saves(monkeypatch)
    use_model(verdict("APPROVE"))

    res = await say(client, body["conversation_id"], "yes")

    assert res.status_code == 200 and res.json()["status"] == "EXECUTED"  # the campaign exists; say so
    assert await campaign_count(session) == 1


async def test_a_button_decision_is_reported_even_if_recording_it_fails(
    client, use_model, make_product, session, monkeypatch
):
    body = await waiting(client, use_model, make_product)
    break_history_saves(monkeypatch)

    res = await client.post(RESUME.format(body["thread_id"]), json={"decision": "APPROVED"})

    assert res.status_code == 200 and res.json()["status"] == "EXECUTED"
    assert await campaign_count(session) == 1
