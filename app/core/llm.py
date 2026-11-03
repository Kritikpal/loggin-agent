"""LangChain configuration: builds the Groq chat model and rotates through the configured API keys."""

import itertools

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_groq import ChatGroq

from app.core.config import get_settings
from app.core.errors import AIUnavailableError

# ponytail: round-robin per model built, in this process. Groq limits each organisation, not each key, so extra
# keys only help across organisations. Upgrade path if 429s persist: on a 429, retry the call on the next key.
_turns = itertools.count()
_http: httpx.AsyncClient | None = None


def chat_model() -> BaseChatModel:
    settings = get_settings()
    keys = settings.groq_api_keys
    if not keys:
        raise AIUnavailableError("The AI assistant is not configured (GROQ_API_KEY is missing).")
    # max_retries=1: the Groq client retries a 429 once after the delay Groq names, and never a 413 (ADR-003).
    return ChatGroq(
        model=settings.groq_model, api_key=keys[next(_turns) % len(keys)], temperature=0, timeout=30, max_retries=1,
        http_async_client=_http_client(),
    )


def _http_client() -> httpx.AsyncClient:
    """One client per process, shared by every model (one is built per chat request), so connections are pooled and
    nothing leaks. Its response hook hands Groq's rate-limit headers to the pacer; ChatGroq does not expose them.
    ponytail: bound to the event loop that first uses it, fine for uvicorn's one loop; a script that calls
    asyncio.run more than once must call close_http_client between runs."""
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(event_hooks={"response": [_observe_rate_limit]})
    return _http


async def close_http_client() -> None:
    """Called on application shutdown (app.main)."""
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


async def _observe_rate_limit(response: httpx.Response) -> None:
    from app.agent.pacer import shared_pacer  # deferred: the agent package imports this module

    shared_pacer().observe(response.headers)
