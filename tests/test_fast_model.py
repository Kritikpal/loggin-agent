"""GROQ_FAST_MODEL: the router and approval check use the fast model; the agent and planner keep the main one."""

import logging
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.api.v1.ai import fast_chat_model
from app.ai.telemetry import LLMCallLogger, llm_step
from app.core.config import get_settings
from tests.scripted_model import ScriptedModel, plan, route, verdict

pytestmark = pytest.mark.usefixtures("tool_session", "checkpointer")

URL = "/api/v1/ai/chat"


@pytest.fixture
def use_fast_model(client):
    def _use(*script) -> ScriptedModel:
        model = ScriptedModel(script=list(script), seen=[])
        client._transport.app.dependency_overrides[fast_chat_model] = lambda: model
        return model

    return _use


async def test_router_runs_on_the_fast_model(client, use_model, use_fast_model):
    main = use_model(AIMessage("We sold nothing at a discount."))
    fast = use_fast_model(route("ANSWER"))

    res = await client.post(URL, json={"message": "How many units did we sell at a discount?"})

    assert res.status_code == 200 and res.json()["response"] == "We sold nothing at a discount."
    assert (len(main.seen), len(fast.seen)) == (1, 1)


async def test_obvious_question_skips_the_router_entirely(client, use_model, use_fast_model):
    main = use_model(AIMessage("We sold nothing at a discount."))
    fast = use_fast_model()

    res = await client.post(URL, json={"message": "How many units did we sell?"})

    assert res.json()["response"] == "We sold nothing at a discount."
    assert (len(main.seen), len(fast.seen)) == (1, 0)


async def test_approval_check_runs_on_the_fast_model(client, use_model, use_fast_model, make_product):
    await make_product(category="Jeans")
    use_model(AIMessage("Jeans are slow."), plan())
    use_fast_model(route("ACTION"))
    waiting = (await client.post(URL, json={"message": "Run a 20% promotion on jeans"})).json()
    assert waiting["status"] == "WAITING_APPROVAL"

    main = use_model()  # approving needs no main-model call
    fast = use_fast_model(verdict("REJECT"))
    res = await client.post(URL, json={"message": "no, cancel it", "conversation_id": waiting["conversation_id"]})

    assert res.json()["status"] == "REJECTED"
    assert (len(main.seen), len(fast.seen)) == (0, 1)


async def test_planner_stays_on_the_main_model(client, use_model, use_fast_model, make_product):
    await make_product(category="Jeans")
    main = use_model(AIMessage("Jeans are slow."), plan())
    fast = use_fast_model(route("ACTION"))

    res = await client.post(URL, json={"message": "Run a 20% promotion on jeans"})

    assert res.json()["status"] == "WAITING_APPROVAL"
    assert (len(main.seen), len(fast.seen)) == (2, 1)  # agent + planner on main, router on fast


def test_fast_model_falls_back_to_the_main_model_when_unset(monkeypatch):
    monkeypatch.setattr(get_settings(), "groq_fast_model", None)
    main = ScriptedModel(script=[], seen=[])

    assert fast_chat_model(main) is main


async def test_call_logger_reports_model_latency_and_tokens(caplog):
    handler, run_id = LLMCallLogger("some-model"), uuid4()
    message = AIMessage("hi", usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})

    with caplog.at_level(logging.INFO, logger="app.ai.telemetry"), llm_step("Decision"):
        await handler.on_chat_model_start({}, [[]], run_id=run_id)
        await handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id)

    assert "step=Decision model=some-model" in caplog.text
    assert "input_tokens=12 output_tokens=3" in caplog.text
