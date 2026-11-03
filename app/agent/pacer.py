"""Wait instead of fail: a token pacer in front of every model call (ADR-003 layer 4).

Groq's free tier allows GROQ_TOKENS_PER_MINUTE (8,000) per organisation, and a hard question sends 5,000-17,000
tokens. Before each call the pacer estimates its tokens (characters / 4 plus an output allowance); if the last 60
seconds plus this call would go over the limit, it waits until the call fits. Groq's own view wins when we have it:
its `x-ratelimit-remaining-tokens` / `x-ratelimit-reset-tokens` response headers (read by an httpx hook in
app.core.llm) also count calls from other processes on the same key. A wait longer than MAX_WAIT ends the question with
BUSY, an ordinary answer, not a 503.

Retries: the Groq client (max_retries=1, app.core.llm) retries a 429 once after the delay Groq names and never retries
a 413. A 429 that survives that retry also ends the question with BUSY; a 413 still fails the request.
"""

import asyncio
import json
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

WINDOW = 60.0  # seconds Groq's token limit spans
MAX_WAIT = 60.0
OUTPUT_ALLOWANCE = 1000  # tokens a reply (with gpt-oss's reasoning) usually stays under
MARGIN = 0.9  # the estimate is characters / 4, not a tokeniser (ADR-003 accepted debt)
BUSY = "The AI is busy right now (rate limit). Please try again in a minute."
_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")


@dataclass
class TokenPacer:
    """One per process, shared by all requests.
    ponytail: in-process state; several uvicorn workers (or a second app) would each think they have the full
    minute. Move the window to a shared store (Redis, Postgres) if the app ever runs more than one process."""

    limit: int
    clock: Callable[[], float] = time.monotonic
    used: deque = field(default_factory=deque)  # (time, tokens) of calls in the last WINDOW seconds
    remaining: tuple[int, float] | None = None  # Groq's (tokens left, time they reset), from the last response
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def wait_for(self, tokens: int) -> float:
        """Seconds until a call of `tokens` fits: the larger of what our window and Groq's headers say."""
        now = self.clock()
        while self.used and self.used[0][0] <= now - WINDOW:
            self.used.popleft()
        wait, total = 0.0, sum(n for _, n in self.used) + tokens
        for at, n in self.used:  # the oldest calls leave the window first
            if total <= self.limit * MARGIN:
                break
            total -= n
            wait = at + WINDOW - now
        if total > self.limit * MARGIN:  # one call bigger than the limit: waiting cannot help, Groq will say 413
            wait = 0.0
        if self.remaining and tokens > self.remaining[0] and self.remaining[1] > now:
            wait = max(wait, self.remaining[1] - now)
        return max(wait, 0.0)

    async def reserve(self, tokens: int) -> bool:
        """Waits until the call fits and books it; False when that would take longer than MAX_WAIT."""
        async with self.lock:  # one caller at a time, so two requests cannot both take the last free tokens
            wait = self.wait_for(tokens)
            if wait > MAX_WAIT:
                return False
            if wait:
                await asyncio.sleep(wait)
            self.used.append((self.clock(), tokens))
            return True

    def settle(self, estimate: int, actual: int | None) -> None:
        """Replaces a booked estimate with the tokens the call really used."""
        if actual is None:
            return
        for i, (at, n) in enumerate(self.used):
            if n == estimate:
                self.used[i] = (at, actual)
                return

    def observe(self, headers: Mapping[str, str]) -> None:
        """Groq's rate-limit headers from a response (an httpx response hook calls this)."""
        left, reset = headers.get("x-ratelimit-remaining-tokens"), headers.get("x-ratelimit-reset-tokens")
        seconds = parse_duration(reset) if reset else None
        if left and left.isdigit() and seconds is not None:
            self.remaining = (int(left), self.clock() + seconds)


def parse_duration(text: str) -> float | None:
    """Groq's reset times: "7.66s", "2m59.56s", "120ms"."""
    match = _DURATION.match(text.strip())
    if not match or not any(match.groups()):
        return None
    minutes, seconds, millis = (float(g) if g else 0.0 for g in match.groups())
    return minutes * 60 + seconds + millis / 1000


_pacer: TokenPacer | None = None


def shared_pacer() -> TokenPacer:
    global _pacer
    if _pacer is None:
        from app.core.config import get_settings  # deferred: settings need the environment

        _pacer = TokenPacer(limit=get_settings().groq_tokens_per_minute)
    return _pacer


def estimate(request: ModelRequest) -> int:
    """Characters / 4 of everything the call sends (system prompt, messages, tool schemas) plus the reply."""
    chars = len(request.system_prompt or "") + sum(len(str(m.content)) + len(str(getattr(m, "tool_calls", "")))
                                                   for m in request.messages)
    for tool in request.tools:
        schema = getattr(tool, "tool_call_schema", None)
        chars += len(getattr(tool, "description", "") or "") + (
            len(json.dumps(schema.model_json_schema())) if hasattr(schema, "model_json_schema") else 0)
    return chars // 4 + OUTPUT_ALLOWANCE


class PaceModelCalls(AgentMiddleware):
    def __init__(self, pacer: TokenPacer | None = None) -> None:
        super().__init__()
        self.pacer = pacer

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        import groq  # deferred like the model client itself

        pacer = self.pacer or shared_pacer()
        tokens = estimate(request)
        if not await pacer.reserve(tokens):
            return ModelResponse(result=[AIMessage(content=BUSY)])
        try:
            response = await handler(request)
        except groq.RateLimitError:  # still 429 after the client's one retry
            return ModelResponse(result=[AIMessage(content=BUSY)])
        usage = next((m.usage_metadata for m in response.result if isinstance(m, AIMessage) and m.usage_metadata),
                     None)
        pacer.settle(tokens, usage["total_tokens"] if usage else None)
        return response
