"""recall(): the saved notes a chat turn is given, and how the agent and planner use them (spec S1, S2, S10)."""

import logging
import uuid
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, SystemMessage
from sqlalchemy import text

from app.ai.memory import content_hash, recall
from app.models import Memory
from app.repositories.memory import MemoryRepository
from tests.scripted_model import call, plan, route

URL = "/api/v1/ai/chat"
MONSOON = "Jeans sales slow down in monsoon, June to September."
WEEKLY = "Wants weekly reports to run Monday to Sunday."
HEADER = "Saved notes from earlier conversations"


def pref(key="report_week", value="Monday to Sunday", content=WEEKLY, **kw) -> Memory:
    return Memory(
        kind="preference", key=key, content=content, evidence="said", content_hash=content_hash(content),
        attributes={"value": value, **kw.pop("attributes", {})}, **kw,
    )


def obs(content=MONSOON, subject="Jeans", **kw) -> Memory:
    return Memory(
        kind="observation", subject=subject, content=content, evidence="said", content_hash=content_hash(content),
        attributes={}, **kw,
    )


async def seed(session, *memories: Memory) -> None:
    MemoryRepository(session).add(*memories)
    await session.commit()


def system_prompt(model, call_index: int = 0) -> str:
    return next(m.text for m in model.seen[call_index] if isinstance(m, SystemMessage))


# ---- recall() ---------------------------------------------------------------------------------------------------


async def test_nothing_saved_means_no_notes(session):
    assert await recall(session, "Should I run a jeans promo in July?", []) == ""


async def test_notes_list_preferences_then_the_observations_that_match(session):
    await seed(
        session,
        pref(),
        pref("default_discount", "15%", "Wants a 15% default discount.", attributes={"origin": "endorsed"}),
        obs(created_at=datetime(2026, 10, 12, tzinfo=UTC)),
        obs("Shirts sell well before the wedding season.", "Shirts"),
        obs("Jeans were popular with students in the past.", status="superseded"),
        obs("Jeans demand may be rising in monsoon.", status="conflicted"),
    )

    notes = await recall(session, "Should I run a jeans promo in July?", [])

    assert notes.startswith(HEADER) and "never a source for numbers" in notes
    assert "When a note bears on the question" in notes and "Otherwise ignore the notes." in notes
    assert notes.index("Preferences:") < notes.index("- default_discount:") < notes.index("- report_week:")
    assert "- report_week: Wants weekly reports to run Monday to Sunday." in notes
    assert "- default_discount: Wants a 15% default discount. (agreed to a suggestion)" in notes
    assert "- [Jeans] Jeans sales slow down in monsoon, June to September. (noted 2026-10-12)" in notes
    assert "Shirts" not in notes and "students" not in notes and "rising" not in notes


async def test_sections_without_entries_are_left_out(session):
    await seed(session, pref())
    assert "Observations:" not in await recall(session, "Hello there", [])

    session.add(obs())
    await session.commit()
    both = await recall(session, "How are jeans doing?", [])
    assert "Preferences:" in both and "Observations:" in both


async def test_s10_a_follow_up_still_finds_the_note_from_the_message_before(session):
    await seed(session, obs())
    history = [{"role": "user", "content": "How are jeans doing?"}, {"role": "assistant", "content": "Steady."}]

    with_context = await recall(session, "And what should I do in July?", history)
    alone = await recall(session, "And what should I do in July?", [])

    assert "[Jeans]" in with_context
    assert alone == ""


async def test_a_failing_lookup_gives_no_notes_and_leaves_the_session_usable(session, monkeypatch):
    async def boom(self):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(MemoryRepository, "active_preferences", boom)

    assert await recall(session, "How are jeans doing?", []) == ""
    assert await session.scalar(text("SELECT 1")) == 1


async def test_the_recall_log_line_shows_what_was_given(session, caplog):
    first, second = pref(), obs()
    await seed(session, first, second)
    caplog.set_level(logging.INFO)
    conv = uuid.uuid4()

    await recall(session, "How are jeans doing?", [], conv)

    line = next(r.message for r in caplog.records if r.message.startswith("memory recall"))
    assert f"conversation={conv}" in line and "preferences=1 observations=1" in line
    assert str(first.id) in line and str(second.id) in line and 'terms="how or are or jeans or doing"' in line


# ---- agent and planner ------------------------------------------------------------------------------------------

flow = pytest.mark.usefixtures("tool_session", "checkpointer")


@flow
async def test_s1_a_new_conversation_follows_a_saved_preference(client, use_model, session):
    await seed(session, pref())
    model = use_model(AIMessage("Last week (Mon-Sun): 3 units."), route("ANSWER"))

    res = await client.post(URL, json={"message": "How did sales go last week?"})

    assert res.status_code == 200
    prompt = system_prompt(model)
    assert HEADER in prompt and "- report_week: Wants weekly reports to run Monday to Sunday." in prompt


@flow
async def test_s2_a_saved_observation_is_used_and_numbers_still_come_from_tools(client, use_model, session):
    await seed(session, obs())
    model = use_model(
        call({"start_date": "2026-07-01", "end_date": "2026-07-31", "category": "Jeans"}),
        AIMessage("July is inside the monsoon slowdown you noted; jeans sold 0 units."),
        route("ANSWER"),
    )

    res = (await client.post(URL, json={"message": "Should I run a jeans promo in July?"})).json()

    prompt = system_prompt(model)
    assert "- [Jeans] Jeans sales slow down in monsoon" in prompt
    assert "say you are relying on it" in prompt  # the model is told to use a relevant note, not just to hold it
    assert [c["name"] for c in res["tool_calls"]] == ["get_sales_trends"]


@flow
async def test_without_memories_the_prompt_has_no_notes(client, use_model):
    model = use_model(AIMessage("Hello!"), route("ANSWER"))

    await client.post(URL, json={"message": "Hi"})

    assert HEADER not in system_prompt(model)


@flow
async def test_the_planner_sees_the_notes_but_the_router_does_not(client, use_model, make_product, session):
    await make_product(category="Jeans")
    await seed(session, pref("default_discount", "15%", "Wants a 15% default discount."))
    model = use_model(AIMessage("Jeans are slow."), route("ACTION"), plan())

    await client.post(URL, json={"message": "Run a promotion on jeans"})

    router_input = "\n".join(m.text for m in model.seen[1])
    planner_input = "\n".join(m.text for m in model.seen[2])
    assert HEADER not in router_input and "default_discount" not in router_input
    assert HEADER in planner_input and "- default_discount: Wants a 15% default discount." in planner_input
    assert "Otherwise ignore the notes." in planner_input
