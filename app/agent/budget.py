"""Per-question tool budgets, enforced in code (app.agent.registry). The prompt asks for the same limits, but only this
makes them certain.

Budgets count calls that ran, not calls requested. LangChain's ToolCallLimitMiddleware counts when the model asks, so a
call with a bad argument (unknown sig, a window over 2 hours) used up the budget without telling the model anything.
Here an argument error comes back as a ToolMessage with status "error" and costs nothing; a refused call is answered
with REFUSED (status "error" too) and never runs. A model that keeps asking is stopped after MAX_TOOL_CALLS + SLACK
requests in all.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

REFUSED = "error: not run"  # the start of a refused call's result; app.agent.graph counts these for its note
SLACK = 2  # requests beyond MAX_TOOL_CALLS that may still fail on arguments before the question is stopped
STOPPED = "I could not finish: the investigation used too many tool calls. Please ask a narrower question."


class ToolBudget(AgentMiddleware):
    def __init__(self, budgets: Mapping[str, int], max_calls: int) -> None:
        super().__init__()
        self.budgets, self.max_calls = dict(budgets), max_calls

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        refusal = self._refusal(request)
        return refusal if refusal else await handler(request)

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        refusal = self._refusal(request)
        return refusal if refusal else handler(request)

    def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        """Counted from the results already in the state: a call ran when its result is not an error.
        ponytail: calls batched into one turn are each checked against earlier turns only, so a batch can overrun by
        its size; gpt-oss-120b never batches (ADR-003). Count in-flight calls per turn if a batching model is used."""
        name = request.tool_call["name"]
        ran = [m.name for m in request.state["messages"] if isinstance(m, ToolMessage) and m.status != "error"]
        limit = self.budgets.get(name)
        if limit is not None and ran.count(name) >= limit:
            why = f"the limit of {limit} {name} call{'s' if limit > 1 else ''} per question is used up"
        elif len(ran) >= self.max_calls:
            why = f"the limit of {self.max_calls} tool calls per question is used up"
        else:
            return None
        return ToolMessage(content=f"{REFUSED}: {why}; answer with what you have", tool_call_id=request.tool_call["id"],
                           name=name, status="error")

    @hook_config(can_jump_to=["end"])
    def after_model(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        """Stops a question whose model keeps asking: refusals and argument errors cost no budget, so without this a
        model could loop on them."""
        messages = state["messages"]
        requested = sum(len(m.tool_calls) for m in messages if isinstance(m, AIMessage))
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or not last.tool_calls or requested <= self.max_calls + SLACK:
            return None
        refused = [ToolMessage(content=f"{REFUSED}: too many tool calls in this question", tool_call_id=call["id"],
                               name=call["name"], status="error") for call in last.tool_calls]
        return {"messages": [*refused, AIMessage(content=STOPPED)], "jump_to": "end"}
