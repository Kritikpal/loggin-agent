"""Tool-call markup that gpt-oss writes into its answer text instead of making a call.

gpt-oss-120b on Groq sometimes ends an answer with its own markup, e.g. `...【assistant<|channel|>commentary
to=functions.find <|message|>{...}`. The user would get a cut-off reply with raw markup, so it is cut off and the answer
says so. ponytail: the call is not recovered and run; re-add that only if the eval shows it matters.
"""

import re
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

LEAK_NOTE = "_Note: the model's reply broke off in the middle of a tool call; ask again for the rest._"
_START = re.compile(r"【assistant|<\|(?:channel|constrain|message|start)\|>")


def strip_leak(message: AIMessage) -> AIMessage:
    """The message with leaked markup cut off and a note added; unchanged when nothing leaked."""
    start = _START.search(message.text)
    if message.tool_calls or start is None:
        return message
    before = message.text[:start.start()].rstrip()
    return message.model_copy(update={"content": f"{before}\n\n{LEAK_NOTE}".strip()})


class StripLeakedToolCalls(AgentMiddleware):
    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        response = await handler(request)
        result = [strip_leak(m) if isinstance(m, AIMessage) else m for m in response.result]
        return ModelResponse(result=result, structured_response=response.structured_response)
