"""A fake chat model for the AI tests: it replays canned replies and records what it was shown. Structured-output
turns (router, planner, approval check) are scripted as a tool call named after the schema."""

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


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


def route(value: str) -> AIMessage:
    """The router's structured-output turn. It always follows the agent's final answer in a script."""
    return call({"route": value}, "r1", tool="Decision")


CAMPAIGN = {
    "name": "Summer Jeans Sale", "message": "20% off all jeans this week.", "category": "jeans", "discount_percent": 20,
}


def plan(action_type: str = "CREATE_CAMPAIGN", **overrides) -> AIMessage:
    """The action planner's structured-output turn. It follows the router's ACTION verdict in a script."""
    fields = {**CAMPAIGN, **overrides} if action_type == "CREATE_CAMPAIGN" else overrides
    return call({"action_type": action_type, **fields}, "p1", tool="ActionPlan")


def extraction(*memories: dict) -> AIMessage:
    """The memory extraction's structured-output turn: the candidates the model found (none = nothing qualifies)."""
    return call({"memories": list(memories)}, "e1", tool="Extraction")


def verdict(value: str) -> AIMessage:
    """The approval check's structured-output turn (APPROVE / REJECT / OTHER)."""
    return call({"decision": value}, "v1", tool="ApprovalVerdict")
