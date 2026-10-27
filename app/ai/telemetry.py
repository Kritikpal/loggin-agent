"""Logs one line per model call (model, latency, tokens) so the cost of a chat turn can be measured."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler

logger = logging.getLogger(__name__)

_step: ContextVar[str] = ContextVar("llm_step", default="?")


@contextmanager
def llm_step(name: str) -> Iterator[None]:
    """Names the step (agent, Decision, ActionPlan, ApprovalVerdict, Extraction) whose model calls run inside."""
    token = _step.set(name)
    try:
        yield
    finally:
        _step.reset(token)


class LLMCallLogger(AsyncCallbackHandler):
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._started: dict[UUID, float] = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id: UUID, **kwargs) -> None:
        self._started[run_id] = perf_counter()

    async def on_llm_end(self, response, *, run_id: UUID, **kwargs) -> None:
        started = self._started.pop(run_id, None)
        message = getattr(response.generations[0][0], "message", None)
        usage = getattr(message, "usage_metadata", None) or {}
        logger.info(
            "llm_call step=%s model=%s ms=%s input_tokens=%s output_tokens=%s",
            _step.get(),
            self.model_name,
            round((perf_counter() - started) * 1000) if started is not None else "?",
            usage.get("input_tokens", "?"),
            usage.get("output_tokens", "?"),
        )

    async def on_llm_error(self, error, *, run_id: UUID, **kwargs) -> None:
        self._started.pop(run_id, None)
