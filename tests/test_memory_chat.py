"""Memory on /api/v1/ai/chat: remember() runs after the reply, off the request, and recall feeds the next
conversation (spec S1, S7). The test client waits for background tasks, so their effects are visible right away."""

import pytest
from langchain_core.messages import AIMessage, SystemMessage
from sqlalchemy import func, select

from app.ai import conversation
from app.api.v1.ai import memory_sessions
from app.models import ChatMessage, Memory
from tests.scripted_model import call, extraction, plan, route, verdict

pytestmark = pytest.mark.usefixtures("tool_session", "checkpointer")

URL = "/api/v1/ai/chat"
WEEKLY = "Always give me weekly reports, Monday to Sunday."
PREFERENCE = {
    "kind": "preference", "updates_id": None, "key": "report_week", "value": "Monday to Sunday", "subject": None,
    "stance": "new", "content": "Wants weekly reports to run Monday to Sunday.", "evidence": WEEKLY,
}


async def memories(session_factory) -> list[Memory]:
    async with session_factory() as s:
        return list(await s.scalars(select(Memory)))


@pytest.fixture
def memory_on(client, session_factory):
    """Turn memory on for the chat endpoint, writing to the test database (it is off by default in tests)."""
    client._transport.app.dependency_overrides[memory_sessions] = lambda: session_factory


async def test_memory_is_off_unless_enabled(client, use_model, session_factory):
    model = use_model(AIMessage("Noted."), extraction(PREFERENCE))

    await client.post(URL, json={"message": WEEKLY})

    assert len(model.seen) == 1 and await memories(session_factory) == []  # the agent only: no router, no extraction


@pytest.mark.usefixtures("memory_on")
async def test_s1_a_preference_said_in_one_conversation_is_followed_in_the_next(client, use_model, session_factory):
    use_model(AIMessage("Noted."), extraction(PREFERENCE))
    first =(await client.post(URL, json={"message": WEEKLY})).json()

    [saved] = await memories(session_factory)
    assert (saved.key, str(saved.source_conversation_id)) == ("report_week", first["conversation_id"])

    model = use_model(AIMessage("Last week (Mon-Sun): 3 units."), extraction())
    second = (await client.post(URL, json={"message": "How did sales go last week?"})).json()

    prompt = next(m.text for m in model.seen[0] if isinstance(m, SystemMessage))
    assert "- report_week: Wants weekly reports to run Monday to Sunday." in prompt
    assert second["conversation_id"] != first["conversation_id"]


@pytest.mark.usefixtures("memory_on")
async def test_s7_a_failed_extraction_changes_nothing_for_the_manager(client, use_model, session_factory):
    use_model(AIMessage("Noted."))  # nothing scripted for the extraction call: it fails

    res = await client.post(URL, json={"message": WEEKLY})

    assert res.status_code == 200 and res.json()["response"] == "Noted."
    assert await memories(session_factory) == []
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(ChatMessage)) == 2  # the turn was still stored


@pytest.mark.usefixtures("memory_on")
async def test_approving_in_chat_does_not_trigger_an_extraction(client, use_model, make_product):
    await make_product(category="Jeans")
    use_model(AIMessage("Jeans are slow."), route("ACTION"), plan(), extraction())
    first = (await client.post(URL, json={"message": "Run a 20% promotion on jeans"})).json()
    assert first["status"] == "WAITING_APPROVAL"

    model = use_model(verdict("APPROVE"))
    second = (await client.post(URL, json={"message": "yes, go ahead with it", "conversation_id": first["conversation_id"]})).json()

    assert second["status"] == "EXECUTED"
    assert len(model.seen) == 1  # only the approval check; a second call would have been the extractor


@pytest.mark.usefixtures("memory_on")
async def test_remember_gets_the_turn_including_what_the_tools_returned(client, use_model, monkeypatch):
    seen: list[dict] = []

    async def fake_remember(model, sessions, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(conversation, "remember", fake_remember)
    use_model(
        call({"start_date": "2025-03-03", "end_date": "2025-03-04"}),
        AIMessage("No sales in that period."),
        route("ANSWER"),
    )

    res = (await client.post(URL, json={"message": "What did we sell in early March 2025?"})).json()

    [kwargs] = seen
    assert kwargs["message"] == "What did we sell in early March 2025?" and kwargs["reply"] == "No sales in that period."
    assert kwargs["status"] == "COMPLETED" and kwargs["history"] == []
    assert kwargs["tool_results"] == res["tool_calls"][0]["result"] != ""
    assert str(kwargs["conversation_id"]) == res["conversation_id"]
