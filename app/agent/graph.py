"""The investigation agent: one model and the read-only tools of app.agent.tools (overview, find, search_logs),
looped by LangGraph's agent runtime.

The budget middleware (app.agent.middleware.budget) is currently detached, so only the prompt asks for the
per-tool limits; nothing enforces them in code.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ContextEditingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.tools import BaseTool

from app.agent.middleware.budget import REFUSED
from app.agent.middleware.leaks import StripLeakedToolCalls
from app.agent.prompt import Spans, build_system_prompt
from app.agent.tools import BUDGETS, MAX_TOOL_CALLS, TOOLS
from app.agent.tools.format import DigestSeenResults, parse_coverage
from app.core.errors import AIUnavailableError
from app.core.llm import chat_model
from app.ingestion.projects import Project
from app.schemas.chat import ToolCall

logger = logging.getLogger(__name__)

Agent = Any  # the compiled LangGraph graph; its generic parameters add nothing here


def build_agent(
    *, model: BaseChatModel | None = None, tools: Sequence[BaseTool] | None = None, project: Project,
    spans: Spans | None = None,
) -> Agent:
    """Build the agent. Everything defaults to the real thing; tests pass a scripted model and fake tools."""
    tools = list(tools) if tools is not None else [spec.make(project) for spec in TOOLS]
    return create_agent(
        model or chat_model(),
        tools,
        system_prompt=build_system_prompt(project, spans),
        middleware=[
            # ToolBudget(BUDGETS, MAX_TOOL_CALLS) is detached for now; re-add here to enforce the per-question limits
            ContextEditingMiddleware(edits=[DigestSeenResults({s.name: s.digest for s in TOOLS})]),  # ADR-003
            StripLeakedToolCalls(),  # gpt-oss sometimes writes tool-call markup into its answer
        ],
    )


@dataclass(frozen=True)
class AgentReply:
    answer: str
    tool_calls: list[ToolCall]


async def ask(agent: Agent, question: str) -> AgentReply:
    """Run one question to its final answer. A model or provider failure is a 503, not a crash."""
    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
    except Exception as exc:
        logger.exception("Agent run failed")
        raise AIUnavailableError("The AI assistant could not complete the request. Try again shortly.") from exc
    messages = result["messages"]
    tool_calls = _tool_calls(messages)
    answer = messages[-1].text + _refused_note(tool_calls) + _coverage_note(tool_calls)
    return AgentReply(answer=answer, tool_calls=tool_calls)


# coverage status -> what it means for the answer; "complete" needs no note
COVERAGE_NOTES = {
    "stale": "the index is stale, newer log lines may be missing",
    "partial": "the newest log lines are not indexed yet",
    "not_indexed": "not indexed, so the answer is unknown, not \"none\"",
    "error": "indexing failed, so results may be incomplete",
}


def _coverage_note(tool_calls: list[ToolCall]) -> str:
    """Said by the code, not the model: the model was told about incomplete coverage and still left it out (eval c3)."""
    statuses: dict[str, str] = {}
    more = 0  # incomplete files the tool did not name (it names at most 10)
    for call in tool_calls:
        coverage = parse_coverage(call.result or "")  # every tool ends with the line app.agent.tools.format.coverage_line
        for status in COVERAGE_NOTES:
            for name in coverage.get(status, []):
                statuses.setdefault(name, status)
        more = max(more, coverage.get("more", 0))
    if not statuses:
        return ""
    parts = "; ".join(f"{file}: {COVERAGE_NOTES[status]}" for file, status in statuses.items())
    if more:
        parts += f"; and {more} more files are not fully indexed either"
    return f"\n\n_Note on coverage: {parts}._"


def _refused_note(tool_calls: list[ToolCall]) -> str:
    """Said by the code, not the model: a call past the budget never ran, and a model will happily describe its
    'result' anyway."""
    refused = sum(1 for call in tool_calls if (call.result or "").startswith(REFUSED))
    if not refused:
        return ""
    what = "tool call was" if refused == 1 else "tool calls were"
    limits = ", ".join(f"{name} {limit}" for name, limit in BUDGETS.items())
    return f"\n\n_Note: {refused} further {what} not run (limits per question: {limits}; {MAX_TOOL_CALLS} in all)._"


def _tool_calls(messages: list[AnyMessage]) -> list[ToolCall]:
    """Pair each tool call the model made with the result it got back, in the order they happened. `turn` numbers the
    model turns that asked for tools, so calls batched into one turn share it (ADR-003)."""
    outcomes = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    turns = [m.tool_calls for m in messages if isinstance(m, AIMessage) and m.tool_calls]
    requested = [(turn, call) for turn, calls in enumerate(turns, 1) for call in calls]
    return [
        ToolCall(
            order=order, turn=turn, name=call["name"], args=call["args"],
            status=outcomes[call["id"]].status if call["id"] in outcomes else "error",
            result=outcomes[call["id"]].text if call["id"] in outcomes else None,
        )
        for order, (turn, call) in enumerate(requested, 1)
    ]
