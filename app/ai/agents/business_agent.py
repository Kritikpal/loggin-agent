"""One tool-calling agent that answers sales and inventory questions from tool data only."""

import logging
from datetime import UTC, date, datetime

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_tool_call
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolInvocationError

from app.ai.tools.inventory import get_inventory_status
from app.ai.tools.sales import get_sales_trends
from app.core.errors import AIUnavailableError
from app.schemas.ai import ChatResponse, ToolCallRecord

logger = logging.getLogger(__name__)

MAX_STEPS = 10  # graph steps (model and tool turns) per request; stops tool-call loops
TOOL_FAILED = "The sales data could not be retrieved because of an internal error."

SYSTEM_PROMPT = """
You are a retail operations analyst.

Today: {today} ({weekday}).
Resolve relative dates and report exact date ranges.

Rules:
- Use tools for all sales and inventory facts.
- Never invent numbers.
- Choose tools based on the user's question.
- Call additional tools only when necessary.
- Distinguish verified findings from possible causes.
- Never treat correlation as causation.
- Inventory represents current stock, not historical stock.
- If data is missing, say so.
- If a tool fails, retry only when the error is actionable.
- Answer unrelated questions without calling tools.
"""

def utc_today() -> date:
    return datetime.now(UTC).date()


@wrap_tool_call
async def _tool_errors_to_model(request, handler):
    """Turn any tool crash into a ToolMessage so the run continues. Bad arguments are explained so the model
    can retry; anything else stays generic so internals never reach the model or the client."""
    try:
        return await handler(request)
    except Exception as exc:
        logger.exception("Tool %s failed", request.tool_call["name"])
        content = exc.message if isinstance(exc, ToolInvocationError) else TOOL_FAILED
        return ToolMessage(content=content, tool_call_id=request.tool_call["id"], status="error")


def build_agent(model: BaseChatModel, today: date):
    return create_agent(
        model,
        tools=[get_sales_trends, get_inventory_status],
        system_prompt=SYSTEM_PROMPT.format(today=today.isoformat(), weekday=today.strftime("%A")),
        middleware=[_tool_errors_to_model],
    )


def _tool_calls(messages: list) -> list[ToolCallRecord]:
    """Pair each requested call (AIMessage) with its outcome (ToolMessage), in call order."""
    records: dict[str, ToolCallRecord] = {}
    for msg in messages:
        if isinstance(msg, AIMessage):
            for call in msg.tool_calls:
                records[call["id"]] = ToolCallRecord(
                    order=len(records) + 1, name=call["name"], args=call["args"], status="error"
                )
        elif isinstance(msg, ToolMessage) and msg.tool_call_id in records:
            records[msg.tool_call_id].status = msg.status or "success"
            records[msg.tool_call_id].result = msg.text
    return list(records.values())


async def chat(model: BaseChatModel, message: str, today: date | None = None) -> ChatResponse:
    agent = build_agent(model, today or utc_today())
    try:
        state = await agent.ainvoke({"messages": [HumanMessage(message)]}, {"recursion_limit": MAX_STEPS})
    except Exception as exc:  # provider outage, rate limit, recursion limit, ...
        logger.exception("Agent run failed")
        raise AIUnavailableError("The AI assistant could not complete the request. Try again shortly.") from exc
    logger.info(state)
    messages = state["messages"]
    final = messages[-1]
    answer = final.text.strip() if isinstance(final, AIMessage) else ""
    if not answer:
        raise AIUnavailableError("The AI assistant returned an empty answer. Try again shortly.")
    calls = _tool_calls(messages)
    for c in calls:  # one debug line per call, in execution order
        logger.info("tool_call #%d %s args=%s status=%s result_chars=%d", c.order, c.name, c.args, c.status,
                    len(c.result or ""))
    return ChatResponse(response=answer, tool_calls=calls)
