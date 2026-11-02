"""Tool calls that gpt-oss writes into its answer text instead of making them.

gpt-oss-120b on Groq sometimes ends an answer with its own tool-call markup, e.g.
    ...log index).【assistant<|channel|>commentary to=functions.summarize_logs <|constrain|>json<|message|>{"levels":...}
The answer stops there: the user gets a cut-off reply with raw markup (eval t2 and q5). Groq returns this as plain text,
so it is repaired after each model call: a call to a known tool with readable arguments becomes a real tool call (the
agent continues, within the same call budgets); anything else is cut off and the answer says so.
"""

import json
import re
import uuid
from collections.abc import Awaitable, Callable, Collection

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage

LEAK_NOTE = "_Note: the model's reply broke off in the middle of a tool call; ask again for the rest._"
_START = re.compile(r"【assistant|<\|(?:channel|constrain|message|start)\|>")
_TOOL = re.compile(r"to=functions\.([\w-]+)")
_ARGS = "<|message|>"


def recover(message: AIMessage, tools: Collection[str]) -> AIMessage:
    """The message with a leaked tool call made real, or stripped with a note; unchanged when nothing leaked."""
    text = message.text
    start = _START.search(text)
    if message.tool_calls or start is None:
        return message
    leak, before = text[start.start():], text[:start.start()].rstrip()
    call = _parse(leak, tools)
    if call is None:
        return message.model_copy(update={"content": f"{before}\n\n{LEAK_NOTE}".strip()})
    return message.model_copy(update={"content": before, "tool_calls": [call]})


def _parse(leak: str, tools: Collection[str]) -> dict | None:
    name, at = _TOOL.search(leak), leak.find(_ARGS)
    if name is None or name.group(1) not in tools or at < 0:
        return None  # an unknown tool is never run, whatever the text asks for
    try:
        args, _ = json.JSONDecoder().raw_decode(leak[at + len(_ARGS):].lstrip())
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None
    return {"name": name.group(1), "args": args, "id": f"recovered-{uuid.uuid4().hex[:12]}", "type": "tool_call"}


class RecoverLeakedToolCalls(AgentMiddleware):
    """Repairs each model reply before the agent acts on it, so the tool-call budgets still apply."""

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        return _repaired(handler(request), request)

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return _repaired(await handler(request), request)


def _repaired(response: ModelResponse, request: ModelRequest) -> ModelResponse:
    names = {getattr(tool, "name", None) or tool.get("name") for tool in request.tools}
    result: list[BaseMessage] = [recover(m, names) if isinstance(m, AIMessage) else m for m in response.result]
    return ModelResponse(result=result, structured_response=response.structured_response)
