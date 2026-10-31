"""The agent loop with a scripted model and a fake tool: no network, no database, no embedding model.
These pin what the code guarantees (the search budget, failure handling, the chat endpoint); whether a real model
follows the prompt is a separate, slow evaluation."""

import uuid
from collections.abc import Iterable

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from app.agent.graph import ask, build_agent
from app.controllers.dependencies import get_agent
from app.core.errors import AIUnavailableError
from app.main import create_app
from app.schemas.chat import ToolCall
from app.schemas.search_logs import LogFilters, SearchLogsRequest

EMPTY_RESULT = '{"results": [], "coverage": []}'


class ScriptedModel(GenericFakeChatModel):
    """Replays the given messages one per model turn; accepts bind_tools like a real chat model."""

    def bind_tools(self, tools, **kwargs):
        return self


class BrokenModel(ScriptedModel):
    def _generate(self, *args, **kwargs):
        raise RuntimeError("provider down: api_key=secret")


def script(*messages: AIMessage) -> ScriptedModel:
    return ScriptedModel(messages=iter(messages))


def search(query: str, call_id: str) -> AIMessage:
    call = {"name": "search_logs", "args": {"query": query}, "id": call_id, "type": "tool_call"}
    return AIMessage(content="", tool_calls=[call])


def fake_tool(calls: list[str]) -> StructuredTool:
    async def run(**arguments) -> str:
        calls.append(arguments["query"])
        return EMPTY_RESULT

    return StructuredTool.from_function(
        coroutine=run, name="search_logs", description="fake", args_schema=SearchLogsRequest)


def agent_for(messages: Iterable[AIMessage], calls: list[str]):
    return build_agent(model=script(*messages), tool=fake_tool(calls), sources=())


async def test_the_agent_searches_then_answers_with_the_models_final_text():
    calls: list[str] = []
    agent = agent_for([search("SPRING_DATASOURCE_URL", "c1"), AIMessage(content="It is unset (L167).")], calls)

    reply = await ask(agent, "why did startup fail?")

    assert reply.answer == "It is unset (L167)."
    assert calls == ["SPRING_DATASOURCE_URL"]
    assert reply.tool_calls == [ToolCall(
        order=1, name="search_logs", args={"query": "SPRING_DATASOURCE_URL"}, status="success", result=EMPTY_RESULT)]


async def test_searches_past_the_budget_are_not_run():
    calls: list[str] = []
    attempts = [search(f"query {n}", f"c{n}") for n in range(1, 6)]
    agent = agent_for([*attempts, AIMessage(content="Gave up.")], calls)

    reply = await ask(agent, "find it")

    assert calls == ["query 1", "query 2", "query 3"]
    assert [(c.order, c.status) for c in reply.tool_calls] == [
        (1, "success"), (2, "success"), (3, "success"), (4, "error"), (5, "error")]


def summarize(call_id: str) -> AIMessage:
    call = {"name": "summarize_logs", "args": {}, "id": call_id, "type": "tool_call"}
    return AIMessage(content="", tool_calls=[call])


def fake_summary_tool(calls: list[str]) -> StructuredTool:
    async def run(**arguments) -> str:
        calls.append("summary")
        return '{"groups": [], "coverage": []}'

    return StructuredTool.from_function(
        coroutine=run, name="summarize_logs", description="fake", args_schema=LogFilters)


async def test_summaries_have_their_own_budget_and_do_not_use_up_searches():
    calls: list[str] = []
    turns = [summarize("s1"), summarize("s2"), summarize("s3"), *(search(f"q{n}", f"c{n}") for n in range(1, 4))]
    agent = build_agent(
        model=script(*turns, AIMessage(content="Done.")), tool=fake_tool(calls),
        summary_tool=fake_summary_tool(calls), sources=())

    reply = await ask(agent, "what is wrong?")

    assert calls == ["summary", "summary", "q1", "q2", "q3"]
    assert "1 further summary was not run (limit of 2 summaries per question)" in reply.answer
    assert "searches" not in reply.answer.split("Done.", 1)[1]


async def test_the_answer_says_when_searches_were_refused_so_the_model_cannot_gloss_over_it():
    calls: list[str] = []
    attempts = [search(f"query {n}", f"c{n}") for n in range(1, 6)]
    agent = agent_for([*attempts, AIMessage(content="Gave up.")], calls)

    answer = (await ask(agent, "find it")).answer

    assert answer.startswith("Gave up.")
    assert "2 further searches were not run" in answer
    assert "limit of 3" in answer


async def test_an_answer_with_every_search_run_gets_no_note():
    agent = agent_for([search("x", "c1"), AIMessage(content="Done.")], [])

    assert (await ask(agent, "q")).answer == "Done."


async def test_a_failing_model_is_a_503_without_leaking_the_provider_error():
    agent = build_agent(model=BrokenModel(messages=iter([])), tool=fake_tool([]), sources=())

    with pytest.raises(AIUnavailableError) as raised:
        await ask(agent, "anything")

    assert "secret" not in raised.value.message


@pytest.fixture
async def chat_client():
    app = create_app()
    app.dependency_overrides[get_agent] = lambda: agent_for(
        [search("HHH000342", "c1"), AIMessage(content="Agent says hi.")], [])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def test_chat_returns_the_answer_the_tool_calls_and_keeps_the_conversation_id(chat_client):
    cid = str(uuid.uuid4())

    res = await chat_client.post("/api/v1/ai/chat", json={"message": "why 500?", "conversation_id": cid})

    assert res.status_code == 200
    assert res.json() == {
        "response": "Agent says hi.", "status": "COMPLETED", "conversation_id": cid,
        "tool_calls": [{"order": 1, "name": "search_logs", "args": {"query": "HHH000342"}, "status": "success",
                        "result": EMPTY_RESULT}],
        "memory_conflicts": [],  # the chat UI reads this field; there is no memory feature behind it yet
    }


@pytest.mark.parametrize("message", ["   ", "\x00\x01\x02", "\n\t "])
async def test_chat_rejects_a_message_with_no_text(chat_client, message):
    assert (await chat_client.post("/api/v1/ai/chat", json={"message": message})).status_code == 422


async def test_chat_strips_control_characters_before_the_agent_sees_them():
    seen: list[str] = []

    class Spy(ScriptedModel):
        def _generate(self, messages, *args, **kwargs):
            seen.append(messages[-1].content)
            return super()._generate(messages, *args, **kwargs)

    app = create_app()
    app.dependency_overrides[get_agent] = lambda: build_agent(
        model=Spy(messages=iter([AIMessage(content="ok")])), tool=fake_tool([]), sources=())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await http.post("/api/v1/ai/chat", json={"message": "  why\x00 500?\x1b  "})

    assert seen == ["why 500?"]
