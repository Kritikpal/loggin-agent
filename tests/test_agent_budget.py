"""The budget middleware keeps every tool visible, then forces a plain answer after a refusal or when the total is used."""

import asyncio
from dataclasses import dataclass, replace
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.middleware.budget import ToolBudget

BUDGETS = {"overview": 1, "find": 2, "search_logs": 2}


@dataclass(frozen=True)
class Request:
    """The two fields of a ModelRequest the middleware reads, with the same immutable override."""

    messages: list
    tools: list

    def override(self, **changes):
        return replace(self, **changes)


TOOLS = [SimpleNamespace(name=name) for name in BUDGETS]


def ran(name: str, status: str = "success", n: int = 1) -> list:
    return [ToolMessage(content="x", tool_call_id=f"{name}{i}{status}", name=name, status=status) for i in range(n)]


def offered(messages: list) -> list[str]:
    seen = []

    async def handler(request):
        seen.extend(t.name for t in request.tools)

    asyncio.run(ToolBudget(BUDGETS, 4).awrap_model_call(Request([AIMessage(content=""), *messages], TOOLS), handler))
    return seen


def test_every_tool_is_offered_before_anything_ran():
    assert offered([]) == ["overview", "find", "search_logs"]


def test_every_tool_stays_visible_when_one_budget_is_used_up():
    # Groq answers 400 when the model calls a tool that is no longer in the request
    assert offered(ran("overview")) == ["overview", "find", "search_logs"]
    assert offered(ran("find", n=2)) == ["overview", "find", "search_logs"]


def test_a_call_that_failed_on_its_arguments_costs_nothing():
    assert offered(ran("overview", "error", n=3)) == ["overview", "find", "search_logs"]


def test_after_a_refusal_the_next_turn_has_no_tools_and_can_only_answer():
    refusal = ToolMessage(content="error: not run: the limit of 2 find calls per question is used up; answer",
                          tool_call_id="r", name="find", status="error")

    assert offered([*ran("find", n=2), refusal]) == []


def test_with_the_total_used_up_the_model_has_no_tools_and_can_only_answer():
    assert offered([*ran("overview"), *ran("find", n=2), *ran("search_logs")]) == []


def test_with_the_total_used_up_the_model_gets_one_plain_message_with_the_results():
    sent = []

    async def handler(request):
        sent.extend(request.messages)

    results = [ToolMessage(content="L140 PKIX failed", tool_call_id="a", name="find"),
               ToolMessage(content="error: bad sig", tool_call_id="b", name="overview", status="error"),
               *ran("find"), *ran("overview"), *ran("search_logs")]
    request = Request([HumanMessage(content="why did logout fail?"), AIMessage(content=""), *results], TOOLS)

    asyncio.run(ToolBudget(BUDGETS, 4).awrap_model_call(request, handler))

    assert len(sent) == 1 and isinstance(sent[0], HumanMessage)
    assert "why did logout fail?" in sent[0].content and "L140 PKIX failed" in sent[0].content
    assert "bad sig" not in sent[0].content and "<results>" in sent[0].content
