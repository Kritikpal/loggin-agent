"""The investigation agent: one model, two read-only tools (search_logs, summarize_logs), looped by LangGraph's agent
runtime.

Each tool's budget (3 searches, 2 summaries) is enforced here, in code: a call past its budget is not run, the model is
told so and must answer with what it has. The prompt asks for the same limits, but only this makes them certain.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ContextEditingMiddleware, ToolCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.tools import BaseTool

from app.agent.context import DigestSeenResults
from app.agent.prompt import Spans, build_system_prompt
from app.agent.tools import SEARCH_BUDGET, SUMMARY_BUDGET, make_search_logs_tool, make_summarize_logs_tool
from app.controllers.dependencies import get_sources
from app.core.errors import AIUnavailableError
from app.core.llm import chat_model
from app.ingestion.sources import Source
from app.schemas.chat import ToolCall

logger = logging.getLogger(__name__)

Agent = Any  # the compiled LangGraph graph; its generic parameters add nothing here


# tool name -> (calls allowed per question, singular, plural) for the budget middleware and the "not run" note
BUDGETS = {"search_logs": (SEARCH_BUDGET, "search", "searches"), "summarize_logs": (SUMMARY_BUDGET, "summary", "summaries")}


def build_agent(
    *, model: BaseChatModel | None = None, tool: BaseTool | None = None, summary_tool: BaseTool | None = None,
    sources: tuple[Source, ...] | None = None, spans: Spans | None = None,
) -> Agent:
    """Build the agent. Everything defaults to the real thing; tests pass a scripted model and fake tools."""
    tools = [tool or make_search_logs_tool(), summary_tool or make_summarize_logs_tool()]
    return create_agent(
        model or chat_model(),
        tools,
        system_prompt=build_system_prompt(get_sources() if sources is None else sources, spans),
        middleware=[
            *(ToolCallLimitMiddleware(tool_name=t.name, run_limit=BUDGETS[t.name][0]) for t in tools),
            ContextEditingMiddleware(edits=[DigestSeenResults()]),  # ADR-003: earlier results go out as digests
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
    for call in tool_calls:
        try:
            coverage = json.loads(call.result or "{}").get("coverage", [])
        except (ValueError, AttributeError):
            continue
        for item in coverage:
            if item.get("status") in COVERAGE_NOTES:
                statuses.setdefault(item["source_id"], item["status"])
    if not statuses:
        return ""
    parts = "; ".join(f"{source}: {COVERAGE_NOTES[status]}" for source, status in statuses.items())
    return f"\n\n_Note on coverage: {parts}._"


def _refused_note(tool_calls: list[ToolCall]) -> str:
    """Said by the code, not the model: a search past the budget never ran, and a model will happily describe its
    'result' anyway."""
    notes = []
    for name, (limit, one, many) in BUDGETS.items():
        refused = sum(1 for call in tool_calls if call.name == name and call.status == "error")
        if refused:
            what = f"{one} was" if refused == 1 else f"{many} were"
            notes.append(f"\n\n_Note: {refused} further {what} not run (limit of {limit} {many} per question)._")
    return "".join(notes)


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
