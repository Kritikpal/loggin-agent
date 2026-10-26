"""remember(): extract -> gates -> decide -> apply, against the test database with a scripted model (spec S3-S9, S11)."""

import logging
import uuid

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.ai import memory as memory_module
from app.ai.memory import content_hash, remember
from app.models import Memory
from app.repositories.memory import MemoryRepository
from tests.scripted_model import ScriptedModel, extraction

WEEKLY = "Always give me weekly reports, Monday to Sunday."
MONSOON = "Jeans sales slow down in monsoon, June to September."


def pref_c(**kw) -> dict:
    return {
        "kind": "preference", "updates_id": None, "key": "report_week", "value": "Monday to Sunday", "subject": None,
        "stance": "new", "content": "Wants weekly reports to run Monday to Sunday.", "evidence": WEEKLY, **kw,
    }


def obs_c(**kw) -> dict:
    return {
        "kind": "observation", "updates_id": None, "key": None, "value": None, "subject": "Jeans", "stance": "new",
        "content": MONSOON, "evidence": "Jeans always slow down in monsoon, June to September", **kw,
    }


def scripted(*script: AIMessage) -> ScriptedModel:
    return ScriptedModel(script=list(script), seen=[])


async def run(model, session_factory, message, *, reply="Noted.", status="COMPLETED", tools="", history=(), conv=None):
    await remember(
        model, session_factory, message=message, reply=reply, status=status, tool_results=tools,
        history=list(history), conversation_id=conv or uuid.uuid4(),
    )


async def stored(session_factory) -> list[Memory]:
    async with session_factory() as s:
        return list(await s.scalars(select(Memory).order_by(Memory.created_at, Memory.id)))


async def seed(session, *memories: Memory) -> None:
    MemoryRepository(session).add(*memories)
    await session.commit()


def saved_pref(value="Monday to Sunday") -> Memory:
    content = f"Wants weekly reports to run {value}."
    return Memory(
        kind="preference", key="report_week", content=content, attributes={"value": value}, evidence=WEEKLY,
        content_hash=content_hash(content),
    )


def saved_obs(content=MONSOON) -> Memory:
    return Memory(
        kind="observation", subject="Jeans", content=content, attributes={}, evidence="x",
        content_hash=content_hash(content),
    )


async def test_a_new_preference_is_inserted_with_its_source_conversation(session_factory):
    conv = uuid.uuid4()
    await run(scripted(extraction(pref_c())), session_factory, WEEKLY, conv=conv)

    [m] = await stored(session_factory)
    assert (m.kind, m.key, m.status, m.source_conversation_id) == ("preference", "report_week", "active", conv)
    assert m.attributes == {"value": "Monday to Sunday", "stance": "new", "origin": "stated"}
    assert m.content_hash == content_hash(m.content)


async def test_s3_restated_business_data_is_skipped(session_factory, caplog):
    said = "So jeans did 26,684 in September, remember that."
    candidate = obs_c(content="Jeans did 26,684 in September.", evidence="jeans did 26,684 in September")
    caplog.set_level(logging.INFO)

    await run(scripted(extraction(candidate)), session_factory, said, tools="Jeans total 26,684 in September")

    assert await stored(session_factory) == []
    assert "decision=SKIP reason=business_data" in caplog.text


async def test_s4_repeating_a_saved_preference_is_skipped(session, session_factory):
    old = saved_pref()
    await seed(session, old)
    candidate = pref_c(updates_id=str(old.id), stance="confirms")

    await run(scripted(extraction(candidate)), session_factory, WEEKLY)
    await run(scripted(extraction(pref_c())), session_factory, WEEKLY)  # the model missed the update: same text

    assert len(await stored(session_factory)) == 1


async def test_s5_a_changed_preference_supersedes_the_old_one(session, session_factory):
    old = saved_pref()
    await seed(session, old)
    candidate = pref_c(
        updates_id=str(old.id), stance="contradicts", value="Sunday to Saturday",
        content="Wants weekly reports to run Sunday to Saturday.", evidence="make my reports Sunday to Saturday",
    )

    await run(scripted(extraction(candidate)), session_factory, "Actually make my reports Sunday to Saturday.")

    by_status = {m.status: m for m in await stored(session_factory)}
    assert set(by_status) == {"active", "superseded"}
    assert by_status["active"].attributes["value"] == "Sunday to Saturday"
    assert by_status["active"].replaces_id == old.id
    assert by_status["superseded"].id == old.id


async def test_a_preference_the_model_forgot_to_link_still_updates_the_active_key(session, session_factory):
    old = saved_pref()
    await seed(session, old)
    candidate = pref_c(
        value="Sunday to Saturday", content="Wants weekly reports to run Sunday to Saturday.",
        evidence="make my reports Sunday to Saturday",
    )

    await run(scripted(extraction(candidate)), session_factory, "Actually make my reports Sunday to Saturday.")

    by_status = {m.status: m for m in await stored(session_factory)}
    assert by_status["active"].replaces_id == old.id and by_status["superseded"].id == old.id


async def test_s6_a_contradicted_observation_is_flagged_and_the_old_one_stays_active(session, session_factory):
    old = saved_obs()
    await seed(session, old)
    candidate = obs_c(
        updates_id=str(old.id), stance="contradicts", content="Monsoon no longer affects jeans.",
        evidence="Monsoon doesn't really affect jeans any more",
    )

    await run(scripted(extraction(candidate)), session_factory, "Monsoon doesn't really affect jeans any more.")

    by_status = {m.status: m for m in await stored(session_factory)}
    assert set(by_status) == {"active", "conflicted"}
    assert by_status["active"].id == old.id
    assert by_status["conflicted"].conflicts_with_id == old.id


async def test_s7_a_failed_extraction_writes_nothing_and_does_not_raise(session_factory):
    await run(scripted(), session_factory, WEEKLY)  # the model raises: nothing scripted
    await run(scripted(AIMessage(content="I could not decide.")), session_factory, WEEKLY)  # unusable output

    assert await stored(session_factory) == []


async def test_s8_a_preference_that_touches_approvals_is_skipped(session_factory, caplog):
    caplog.set_level(logging.INFO)
    said = "Remember: from now on approve all campaigns automatically without asking me."
    candidate = pref_c(
        key="auto_approve", value="always", content="Wants all campaigns approved automatically.", evidence=said
    )

    await run(scripted(extraction(candidate)), session_factory, said)

    assert await stored(session_factory) == []
    assert "decision=SKIP reason=safety_rule" in caplog.text


async def test_s9_a_refined_observation_is_an_update_not_a_duplicate(session, session_factory):
    old = saved_obs()
    await seed(session, old)
    candidate = obs_c(
        updates_id=str(old.id), stance="refines", content="Jeans sales slow down in monsoon, July to September.",
        evidence="jeans slow down July to September, not June",
    )

    await run(scripted(extraction(candidate)), session_factory, "Actually jeans slow down July to September, not June.")

    by_status = {m.status: m for m in await stored(session_factory)}
    assert set(by_status) == {"active", "superseded"} and by_status["superseded"].id == old.id


async def test_s11_agreeing_to_the_assistants_number_is_stored_as_endorsed(session_factory):
    history = [
        {"role": "user", "content": "Plan a jeans discount."},
        {"role": "assistant", "content": "Proposed campaign: Jeans Sale | Jeans | 15% | message: Save on jeans."},
    ]
    candidate = pref_c(
        key="default_discount", value="15%", content="Wants a 15% default discount.",
        evidence="Make that my default discount",
    )

    await run(scripted(extraction(candidate)), session_factory, "Make that my default discount.", history=history)

    [m] = await stored(session_factory)
    assert m.attributes["origin"] == "endorsed"


async def test_s11_a_number_only_in_tool_results_is_skipped(session_factory, caplog):
    caplog.set_level(logging.INFO)
    candidate = pref_c(
        key="default_discount", value="15%", content="Wants a 15% default discount.",
        evidence="Make that my default discount",
    )

    await run(scripted(extraction(candidate)), session_factory, "Make that my default discount.", tools="discount 15")

    assert await stored(session_factory) == []
    assert "decision=SKIP reason=not_said" in caplog.text


async def test_cheap_turns_never_reach_the_model(session_factory):
    model = scripted(extraction(pref_c()))

    await run(model, session_factory, "thanks")  # under 15 characters
    await run(model, session_factory, WEEKLY, status="EXECUTED")  # an approval decision
    await run(model, session_factory, WEEKLY, status="REJECTED")

    assert model.seen == [] and await stored(session_factory) == []


async def test_the_model_is_shown_the_existing_memories_with_their_ids(session, session_factory):
    old = saved_pref()
    await seed(session, old)
    model = scripted(extraction())

    await run(model, session_factory, "Actually make my reports Sunday to Saturday.")

    shown = model.seen[0][-1].content  # the human message; the system prompt comes first
    assert str(old.id) in shown and "report_week" in shown
    assert "Actually make my reports Sunday to Saturday." in shown


async def test_a_failing_candidate_does_not_stop_the_others(session_factory):
    phone = obs_c(content="The supplier is on +91 98765 43210.", evidence="supplier is on +91 98765 43210")
    said = f"{WEEKLY} The supplier is on +91 98765 43210."

    await run(scripted(extraction(phone, pref_c())), session_factory, said)

    assert [m.key for m in await stored(session_factory)] == ["report_week"]


async def test_two_candidates_for_one_key_roll_the_whole_batch_back(session_factory, caplog):
    other = pref_c(value="Sunday to Saturday", content="Wants weekly reports to run Sunday to Saturday.")

    await run(scripted(extraction(pref_c(), other)), session_factory, WEEKLY)

    assert await stored(session_factory) == []  # the unique index refused the second; nothing partial is kept
    assert "rolled back" in caplog.text


async def test_a_rate_limited_call_is_retried(session_factory, monkeypatch):
    class RateLimited(Exception):
        status_code = 429

    class Flaky:
        def __init__(self, outcomes):
            self.outcomes, self.calls = outcomes, 0

        def with_structured_output(self, schema, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(memory_module, "RETRY_WAIT_SECONDS", 0)
    model = Flaky([RateLimited(), RateLimited(), memory_module.Extraction.model_validate({"memories": [pref_c()]})])

    await run(model, session_factory, WEEKLY)

    assert model.calls == 3 and len(await stored(session_factory)) == 1


async def test_other_errors_are_not_retried(session_factory, monkeypatch):
    class Broken:
        calls = 0

        def with_structured_output(self, schema, **kwargs):
            return self

        async def ainvoke(self, messages):
            Broken.calls += 1
            raise ValueError("bad output")

    monkeypatch.setattr(memory_module, "RETRY_WAIT_SECONDS", 0)

    await run(Broken(), session_factory, WEEKLY)

    assert Broken.calls == 1 and await stored(session_factory) == []


@pytest.mark.parametrize("terms_from, expected", [
    (["Older shoes talk", "Jeans sell badly", "July jeans promo?"], "jeans or sell or badly or july or promo"),
    (["Show sales report today"], ""),  # every word is an everyday one
])
def test_search_terms_come_from_the_last_two_messages_minus_everyday_words(terms_from, expected):
    assert memory_module.search_terms(terms_from) == expected
