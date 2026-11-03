"""The agent loop with a scripted model and a fake tool: no network, no database, no embedding model.
These pin what the code guarantees (the search budget, failure handling, the chat endpoint); whether a real model
follows the prompt is a separate, slow evaluation."""

import json
import uuid
from collections.abc import Iterable

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from app.agent.graph import ask, build_agent
from app.controllers.dependencies import get_agent, get_projects
from app.core.errors import AIUnavailableError
from app.main import create_app
from app.schemas.chat import ToolCall
from app.schemas.search_logs import LogFilters, SearchLogsRequest

from tests.project_helpers import make_project

PROJECT = make_project("demo")
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
    return build_agent(model=script(*messages), tool=fake_tool(calls), project=PROJECT)


async def test_the_agent_searches_then_answers_with_the_models_final_text():
    calls: list[str] = []
    agent = agent_for([search("SPRING_DATASOURCE_URL", "c1"), AIMessage(content="It is unset (L167).")], calls)

    reply = await ask(agent, "why did startup fail?")

    assert reply.answer == "It is unset (L167)."
    assert calls == ["SPRING_DATASOURCE_URL"]
    assert reply.tool_calls == [ToolCall(
        order=1, turn=1, name="search_logs", args={"query": "SPRING_DATASOURCE_URL"}, status="success",
        result=EMPTY_RESULT)]


async def test_calls_requested_together_share_a_turn_so_batching_is_visible():
    calls: list[str] = []
    together = AIMessage(content="", tool_calls=[
        {"name": "search_logs", "args": {"query": "PID"}, "id": "c1", "type": "tool_call"},
        {"name": "summarize_logs", "args": {}, "id": "s1", "type": "tool_call"}])
    agent = build_agent(
        model=script(together, search("Caused by", "c2"), AIMessage(content="Done.")), tool=fake_tool(calls),
        summary_tool=fake_summary_tool(calls), project=PROJECT)

    reply = await ask(agent, "two independent parts")

    assert [(c.order, c.turn, c.name) for c in reply.tool_calls] == [
        (1, 1, "search_logs"), (2, 1, "summarize_logs"), (3, 2, "search_logs")]


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
        summary_tool=fake_summary_tool(calls), project=PROJECT)

    reply = await ask(agent, "what is wrong?")

    assert calls == ["summary", "summary", "q1", "q2", "q3"]
    assert "1 further summary was not run (limit of 2 summaries per question)" in reply.answer
    assert "searches" not in reply.answer.split("Done.", 1)[1]


def coverage_tool(statuses: dict[str, str]) -> StructuredTool:
    async def run(**arguments) -> str:
        coverage = {"files": len(statuses), "indexed": len(statuses)}
        for name, status in statuses.items():
            if status != "complete":
                coverage.setdefault(status, []).append(name)
        return json.dumps({"groups": [], "coverage": coverage})

    return StructuredTool.from_function(
        coroutine=run, name="summarize_logs", description="fake", args_schema=LogFilters)


async def test_the_answer_says_when_a_source_is_not_fully_indexed_whatever_the_model_wrote():
    agent = build_agent(
        model=script(summarize("s1"), AIMessage(content="There are 3 errors.")), tool=fake_tool([]),
        summary_tool=coverage_tool({"a": "stale", "b": "complete", "c": "not_indexed"}), project=PROJECT)

    answer = (await ask(agent, "how many errors?")).answer

    assert answer.startswith("There are 3 errors.")
    assert "a: the index is stale" in answer and "c: not indexed" in answer
    assert "b:" not in answer


async def test_complete_coverage_adds_no_note():
    agent = build_agent(
        model=script(summarize("s1"), AIMessage(content="Done.")), tool=fake_tool([]),
        summary_tool=coverage_tool({"a": "complete"}), project=PROJECT)

    assert (await ask(agent, "q")).answer == "Done."


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
    agent = build_agent(model=BrokenModel(messages=iter([])), tool=fake_tool([]), project=PROJECT)

    with pytest.raises(AIUnavailableError) as raised:
        await ask(agent, "anything")

    assert "secret" not in raised.value.message


@pytest.fixture
async def chat_client():
    app = create_app()
    app.dependency_overrides[get_projects] = lambda: (PROJECT, make_project("broken", error="bad yaml"))
    app.dependency_overrides[get_agent] = lambda: agent_for(
        [search("HHH000342", "c1"), AIMessage(content="Agent says hi.")], [])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def test_chat_returns_the_answer_the_tool_calls_and_keeps_the_conversation_id(chat_client):
    cid = str(uuid.uuid4())

    res = await chat_client.post("/api/v1/projects/demo/chat", json={"message": "why 500?", "conversation_id": cid})

    assert res.status_code == 200
    assert res.json() == {
        "response": "Agent says hi.", "status": "COMPLETED", "conversation_id": cid,
        "tool_calls": [{"order": 1, "turn": 1, "name": "search_logs", "args": {"query": "HHH000342"},
                        "status": "success", "result": EMPTY_RESULT}],
        "memory_conflicts": [],  # the chat UI reads this field; there is no memory feature behind it yet
    }


@pytest.mark.parametrize("message", ["   ", "\x00\x01\x02", "\n\t "])
async def test_chat_rejects_a_message_with_no_text(chat_client, message):
    assert (await chat_client.post("/api/v1/projects/demo/chat", json={"message": message})).status_code == 422


async def test_chat_strips_control_characters_before_the_agent_sees_them():
    seen: list[str] = []

    class Spy(ScriptedModel):
        def _generate(self, messages, *args, **kwargs):
            seen.append(messages[-1].content)
            return super()._generate(messages, *args, **kwargs)

    app = create_app()
    app.dependency_overrides[get_projects] = lambda: (PROJECT,)
    app.dependency_overrides[get_agent] = lambda: build_agent(
        model=Spy(messages=iter([AIMessage(content="ok")])), tool=fake_tool([]), project=PROJECT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await http.post("/api/v1/projects/demo/chat", json={"message": "  why\x00 500?\x1b  "})

    assert seen == ["why 500?"]


class RecordingModel(ScriptedModel):
    """Remembers the messages each model call was sent."""

    seen: list = []

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


async def test_the_model_gets_earlier_results_as_digests_but_the_reply_keeps_them_in_full():
    long_hit = {"source_id": "s", "occurred_at": None, "level": "ERROR", "line_start": 158, "line_end": 300,
                "raw_text": "Driver claims to not accept jdbcUrl\n" + "\tat a.b(C.java:1)\n" * 150 + "Caused by: ROOT",
                "score": 0.1, "matched_by": ["keyword"]}
    full = json.dumps({"results": [long_hit], "coverage": []})

    async def run(**arguments) -> str:
        return full

    tool = StructuredTool.from_function(coroutine=run, name="search_logs", description="fake",
                                        args_schema=SearchLogsRequest)
    model = RecordingModel(messages=iter([search("a", "c1"), search("b", "c2"), AIMessage(content="Done.")]))
    model.seen = []

    reply = await ask(build_agent(model=model, tool=tool, summary_tool=fake_summary_tool([]), project=PROJECT), "why?")

    second_call, last_call = model.seen[1], model.seen[2]
    assert [m.content for m in second_call if m.type == "tool"] == [full]  # not read yet: full
    first, latest = [m.content for m in last_call if m.type == "tool"]
    assert len(first) < 600 and "L158-300" in first and "Caused by: ROOT" in first  # read before: digest
    assert latest == full
    assert [c.result for c in reply.tool_calls] == [full, full]  # the API still shows everything


async def test_a_tool_call_leaked_into_the_answer_text_is_run_as_a_real_call():
    calls: list[str] = []
    leaked = AIMessage(content='18 errors so far.【assistant to=functions.search_logs<|channel|>commentary '
                               '<|constrain|>json<|message|>{"query":"UserNotFound"}')
    agent = build_agent(model=script(leaked, AIMessage(content="User 35eea954 does not exist.")),
                        tool=fake_tool(calls), summary_tool=fake_summary_tool([]), project=PROJECT)

    reply = await ask(agent, "which user?")

    assert calls == ["UserNotFound"]
    assert reply.answer == "User 35eea954 does not exist."
    assert [(c.name, c.args, c.status) for c in reply.tool_calls] == [("search_logs", {"query": "UserNotFound"}, "success")]


async def test_recovered_calls_still_count_against_the_search_budget():
    calls: list[str] = []
    leak = '【assistant to=functions.search_logs<|channel|>commentary <|constrain|>json<|message|>{{"query":"q{}"}}'
    agent = build_agent(model=script(*(AIMessage(content=leak.format(n)) for n in range(1, 5)),
                                     AIMessage(content="Gave up.")),
                        tool=fake_tool(calls), summary_tool=fake_summary_tool([]), project=PROJECT)

    reply = await ask(agent, "find it")

    assert calls == ["q1", "q2", "q3"]
    assert [c.status for c in reply.tool_calls] == ["success", "success", "success", "error"]


async def test_chat_on_an_unknown_project_is_404(chat_client):
    res = await chat_client.post("/api/v1/projects/nope/chat", json={"message": "why?"})
    assert res.status_code == 404 and res.json()["error"]["message"] == "No such project."


async def test_chat_on_an_invalid_project_is_409(chat_client):
    res = await chat_client.post("/api/v1/projects/broken/chat", json={"message": "why?"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "project_invalid"


async def test_old_chat_route_is_gone(chat_client):
    assert (await chat_client.post("/api/v1/ai/chat", json={"message": "why?"})).status_code == 404


async def test_build_agent_tools_only_see_the_project(tmp_path):
    from app.ingestion.sources import FileSource

    project = make_project("a", files=[FileSource(id="a/x", path=tmp_path / "x.log")])
    call = {"name": "search_logs", "args": {"query": "q", "source_ids": ["b/other"]}, "id": "c1", "type": "tool_call"}
    agent = build_agent(model=script(AIMessage(content="", tool_calls=[call]), AIMessage(content="Done.")),
                        project=project)

    reply = await ask(agent, "why?")

    assert "invalid search arguments" in reply.tool_calls[0].result and "source_ids" in reply.tool_calls[0].result


async def test_the_coverage_note_says_how_many_more_files_are_incomplete():
    async def run(**arguments) -> str:
        return json.dumps({"groups": [], "coverage": {"files": 14, "indexed": 0, "not_indexed": ["a", "b"], "more": 12}})

    tool = StructuredTool.from_function(coroutine=run, name="summarize_logs", description="fake", args_schema=LogFilters)
    agent = build_agent(model=script(summarize("s1"), AIMessage(content="None.")), tool=fake_tool([]),
                        summary_tool=tool, project=PROJECT)

    answer = (await ask(agent, "how many errors?")).answer

    assert "a: not indexed" in answer and "12 more files" in answer
