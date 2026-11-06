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


# --- count_by in the registry ---


def registry():
    from app.agent.tools import BUDGETS as REGISTRY
    from app.agent.tools import MAX_TOOL_CALLS, TOOLS as SPECS

    return REGISTRY, MAX_TOOL_CALLS, SPECS


def offered_from(budgets: dict, messages: list) -> list[str]:
    seen: dict = {}

    async def handler(request):
        seen["names"] = [t.name for t in request.tools]

    request = Request([AIMessage(content=""), *messages], [SimpleNamespace(name=name) for name in budgets])
    asyncio.run(ToolBudget(budgets, registry()[1]).awrap_model_call(request, handler))
    return seen["names"]


def test_count_by_is_registered_with_a_budget_of_two():
    budgets, cap, specs = registry()

    assert [spec.name for spec in specs] == ["overview", "find", "search_logs", "count_by"]
    assert budgets["count_by"] == 2 and cap == 4


def test_the_scorer_budgets_match_the_registry():
    from scripts.eval_scoring import BUDGETS as SCORER

    assert SCORER == registry()[0]


def test_count_by_stays_visible_after_two_calls_and_all_tools_go_when_the_total_is_used():
    budgets = registry()[0]

    assert offered_from(budgets, ran("count_by", n=2)) == list(budgets)
    assert offered_from(budgets, [*ran("count_by", n=2), *ran("find", n=2)]) == []


def test_digest_count_keeps_the_header_ten_rows_the_notes_and_the_coverage():
    from app.agent.tools.format import DIGEST_MARK, digest_count

    header = "window all · filters none · by user · total 300 · groups 30 · columns: user | count | first | last"
    rows = [f"user{i} | {30 - i} | L{i}@10:00Z | L{i}@11:00Z" for i in range(30)]
    result = "\n".join([header, *rows, "note: 20 more groups; raise top (max 50) or narrow the window", "files 1, indexed 1"])

    lines = digest_count(result).splitlines()

    assert lines[:2] == [DIGEST_MARK, header] and lines[2:12] == rows[:10]
    assert lines[12].startswith("note: 20 more groups") and lines[-1] == "files 1, indexed 1" and len(lines) == 14


def test_digest_count_leaves_errors_short_results_and_digests_alone():
    from app.agent.tools.format import digest_count

    short = "\n".join(["window all · by user · total 2 · groups 2 · columns: user | count | first | last",
                       "a | 1 | L1@10:00Z | L1@10:00Z", "files 1, indexed 1"])
    long = "\n".join([short.splitlines()[0], *[f"u{i} | 1 | L1@10:00Z | L1@10:00Z" for i in range(30)], "files 1, indexed 1"])

    assert digest_count("error: count_by is temporarily unavailable") is None
    assert digest_count(short) is None
    assert digest_count(digest_count(long)) is None
