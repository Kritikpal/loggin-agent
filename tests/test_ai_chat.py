"""POST /api/v1/ai/chat with a scripted model: no network, real tool, real (test) database."""

import json
from datetime import UTC, date, datetime

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.ai.agents import business_agent
from app.api.v1.ai import chat_model
from app.core.config import get_settings
from app.services.sale import SaleService

pytestmark = pytest.mark.usefixtures("tool_session")

URL = "/api/v1/ai/chat"


class ScriptedModel(BaseChatModel):
    """Replays canned AIMessages and records the messages it was shown on each turn."""

    script: list[AIMessage]
    seen: list[list[BaseMessage]]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        step = len(self.seen) - 1
        if step >= len(self.script):
            raise RuntimeError("model called more often than scripted")
        return ChatResult(generations=[ChatGeneration(message=self.script[step])])


def call(args: dict, call_id: str = "c1", tool: str = "get_sales_trends") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": tool, "args": args, "id": call_id}])


def stock_call(args: dict, call_id: str = "c1") -> AIMessage:
    return call(args, call_id, tool="get_inventory_status")


@pytest.fixture
def use_model(client):
    def _use(*script: AIMessage) -> ScriptedModel:
        model = ScriptedModel(script=list(script), seen=[])
        client._transport.app.dependency_overrides[chat_model] = lambda: model
        return model

    return _use


def tool_outputs(model: ScriptedModel) -> list[ToolMessage]:
    return [m for m in model.seen[-1] if isinstance(m, ToolMessage)]


async def test_agent_picks_arguments_and_answer_is_grounded_in_tool_data(client, use_model, make_product, add_sale):
    product = await make_product(price="10.00")
    await add_sale(product, datetime(2025, 3, 3, 12, tzinfo=UTC), quantity=3)
    model = use_model(call({"start_date": "2025-03-03", "end_date": "2025-03-04"}), AIMessage("3 units, 30.00."))

    res = await client.post(URL, json={"message": "How did we sell on Mar 3-4?"})

    assert res.status_code == 200
    body = res.json()
    assert body["response"] == "3 units, 30.00."
    [record] = body["tool_calls"]
    assert (record["order"], record["name"], record["status"]) == (1, "get_sales_trends", "success")
    assert record["args"] == {"start_date": "2025-03-03", "end_date": "2025-03-04"}
    points = json.loads(record["result"])["points"]
    assert [(p["period"], p["revenue"], p["units"]) for p in points] == [
        ("2025-03-03", "30.00", 3), ("2025-03-04", "0.00", 0),
    ]
    assert tool_outputs(model)[0].content == record["result"]  # the model saw exactly the reported data


async def test_system_prompt_carries_current_utc_date(client, use_model, monkeypatch):
    monkeypatch.setattr(business_agent, "utc_today", lambda: date(2026, 10, 21))
    model = use_model(AIMessage("Hello."))

    await client.post(URL, json={"message": "hi"})

    system = next(m for m in model.seen[0] if isinstance(m, SystemMessage))
    assert "2026-10-21 (Wednesday)" in system.text


async def test_tool_business_error_is_reported_and_model_can_recover(client, use_model):
    model = use_model(
        call({"start_date": "2000-01-01", "end_date": "2025-01-01"}),  # too many daily buckets
        call({"start_date": "2025-01-01", "end_date": "2025-01-02"}, "c2"),
        AIMessage("No sales on those days."),
    )

    body = (await client.post(URL, json={"message": "everything"})).json()

    assert [c["status"] for c in body["tool_calls"]] == ["error", "success"]
    assert "narrow it" in body["tool_calls"][0]["result"]
    assert body["response"] == "No sales on those days."
    assert len(model.seen) == 3


async def test_invalid_model_arguments_are_returned_to_the_model(client, use_model):
    model = use_model(call({"start_date": "last week", "end_date": "2025-03-05"}), AIMessage("Could not fetch."))

    res = await client.post(URL, json={"message": "last week?"})

    assert res.status_code == 200
    assert res.json()["tool_calls"][0]["status"] == "error"
    assert "start_date" in tool_outputs(model)[0].content


async def test_unexpected_tool_crash_is_masked_and_not_fatal(client, use_model, monkeypatch):
    async def boom(*_, **__):
        raise RuntimeError("password=hunter2 db exploded")

    monkeypatch.setattr(SaleService, "trends", boom)
    model = use_model(call({"start_date": "2025-03-03", "end_date": "2025-03-04"}), AIMessage("Data unavailable."))

    res = await client.post(URL, json={"message": "sales?"})

    assert res.status_code == 200
    record = res.json()["tool_calls"][0]
    assert record["status"] == "error"
    assert record["result"] == business_agent.TOOL_FAILED
    assert "hunter2" not in res.text
    assert "hunter2" not in tool_outputs(model)[0].content


async def test_no_tool_scenario_answers_directly(client, use_model):
    model = use_model(AIMessage("I can help with sales and inventory questions."))

    body = (await client.post(URL, json={"message": "what is the weather"})).json()

    assert body["tool_calls"] == []
    assert body["response"].startswith("I can help")
    assert len(model.seen) == 1


async def test_single_inventory_tool_scenario(client, use_model, make_product):
    await make_product(category="Jeans", sku="JN-1", quantity=2, reorder_level=5)
    await make_product(category="Jeans", sku="JN-2", quantity=40, reorder_level=5)
    use_model(stock_call({"category": "jeans", "low_stock_only": True}), AIMessage("JN-1 is low."))

    body = (await client.post(URL, json={"message": "Which jeans need restocking?"})).json()

    [record] = body["tool_calls"]
    assert (record["order"], record["name"], record["status"]) == (1, "get_inventory_status", "success")
    assert record["args"] == {"category": "jeans", "low_stock_only": True}
    result = json.loads(record["result"])
    assert [(i["sku"], i["quantity"], i["reorder_level"], i["stock_status"]) for i in result["items"]] == [
        ("JN-1", 2, 5, "low_stock"),
    ]


async def test_multi_tool_scenario_second_call_follows_first_result(client, use_model, make_product, add_sale):
    jeans = await make_product(category="Jeans", sku="JN-1", quantity=0, reorder_level=5)
    await add_sale(jeans, datetime(2025, 3, 3, 12, tzinfo=UTC), quantity=4)
    model = use_model(
        call({"start_date": "2025-03-03", "end_date": "2025-03-04", "category": "Jeans"}),
        stock_call({"category": "Jeans"}, "c2"),
        AIMessage("Verified: 4 units sold; JN-1 is out of stock now. Possible cause (unverified): stockout."),
    )

    body = (await client.post(URL, json={"message": "Why did jeans sales stop?"})).json()

    assert [(c["order"], c["name"], c["status"]) for c in body["tool_calls"]] == [
        (1, "get_sales_trends", "success"), (2, "get_inventory_status", "success"),
    ]
    assert json.loads(body["tool_calls"][1]["result"])["items"][0]["stock_status"] == "out_of_stock"
    assert len(model.seen) == 3
    # the final model turn saw both results, in order
    assert [m.content for m in tool_outputs(model)] == [c["result"] for c in body["tool_calls"]]


async def test_multi_tool_one_failure_does_not_hide_the_other_result(client, use_model, monkeypatch):
    from app.services.inventory import InventoryService

    async def boom(*_, **__):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(InventoryService, "list", boom)
    use_model(
        call({"start_date": "2025-03-03", "end_date": "2025-03-04"}),
        stock_call({}, "c2"),
        AIMessage("Sales fetched; stock data unavailable."),
    )

    res = await client.post(URL, json={"message": "sales and stock"})

    assert [(c["name"], c["status"]) for c in res.json()["tool_calls"]] == [
        ("get_sales_trends", "success"), ("get_inventory_status", "error"),
    ]
    assert "secret internals" not in res.text


def test_system_prompt_describes_both_tools_and_evidence_labels():
    prompt = business_agent.SYSTEM_PROMPT
    for needle in ("get_sales_trends", "get_inventory_status", "Verified", "Possible causes (not verified)"):
        assert needle in prompt


async def test_provider_failure_returns_503_envelope(client, use_model):
    use_model()  # empty script: the first model call raises

    res = await client.post(URL, json={"message": "sales?"})

    assert res.status_code == 503
    assert res.json()["error"]["code"] == "ai_unavailable"


async def test_runaway_tool_loop_is_stopped(client, use_model):
    use_model(*[call({"start_date": "2025-03-03", "end_date": "2025-03-04"}, f"c{i}") for i in range(30)])
    res = await client.post(URL, json={"message": "loop"})
    assert res.status_code == 503


async def test_missing_api_key_returns_503(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "groq_api_key", None)
    res = await client.post(URL, json={"message": "sales?"})
    assert res.status_code == 503
    assert "GROQ_API_KEY" in res.json()["error"]["message"]


def test_configured_key_builds_groq_model(monkeypatch):
    monkeypatch.setattr(get_settings(), "groq_api_key", "test-key")
    assert chat_model().model_name == get_settings().groq_model


@pytest.mark.parametrize(
    "body", [{}, {"message": ""}, {"message": "   "}, {"message": "x" * 2001}, {"message": "a", "x": 1}]
)
async def test_invalid_request_is_rejected(client, use_model, body):
    use_model()
    res = await client.post(URL, json=body)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "validation_error"
