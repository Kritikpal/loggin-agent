"""Everything that talks to the LLM provider: building the models and the shared structured-output call.
The agent's tool loop (agents/business_agent.py) uses these models through LangChain's create_agent."""

import asyncio
import itertools
import logging

from fastapi import Depends
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from pydantic import BaseModel

from app.ai.telemetry import LLMCallLogger, llm_step
from app.core.config import get_settings
from app.core.errors import AIUnavailableError

logger = logging.getLogger(__name__)

RATE_LIMITED = 429
# ponytail: round-robin per model built, in this process. A request builds up to three (agent+planner, router and
# approval check, background memory extraction), so one chat already spreads over the keys. Groq limits each
# organisation, not each key: keys from one organisation share a limit, so this only helps across organisations.
# Upgrade path if 429s persist: on a 429, retry the call on the next key instead of waiting.
_turns = itertools.count()


def _groq(name: str, keys: list[str]) -> BaseChatModel:
    index = next(_turns) % len(keys)
    return ChatGroq(
        model=name, api_key=keys[index], temperature=0, timeout=30, max_retries=1,
        callbacks=[LLMCallLogger(f"{name} key{index + 1}")],  # which key, never the key itself
    )


def chat_model() -> BaseChatModel:
    settings = get_settings()
    if not settings.groq_api_keys:
        raise AIUnavailableError("The AI assistant is not configured (GROQ_API_KEY is missing).")
    return _groq(settings.groq_model, settings.groq_api_keys)


def fast_chat_model(model: BaseChatModel = Depends(chat_model)) -> BaseChatModel:
    """The model for the router and approval check: GROQ_FAST_MODEL when set, else the main model."""
    settings = get_settings()
    if not settings.groq_fast_model:
        return model
    return _groq(settings.groq_fast_model, settings.groq_api_keys)  # chat_model already checked there are keys


async def structured_call[T: BaseModel](
    model: BaseChatModel, schema: type[T], system: str, human: str, *,
    strict: bool = False, retries: int = 0, retry_wait: float = 0,
) -> T | None:
    """One structured-output call. None when the output is unusable: invalid, missing, or the call itself failed.
    A 429 is retried up to `retries` times, `retry_wait` seconds apart; any other error is not retried.
    json_schema, not the function-calling default: that forces a tool call, and Groq answers 400
    ("Tool choice is required") when the model just replies to something like "Hi"."""
    name = schema.__name__
    kwargs = {"method": "json_schema", **({"strict": True} if strict else {})}
    for attempt in range(retries + 1):
        try:
            with llm_step(name):
                result = await model.with_structured_output(schema, **kwargs).ainvoke(
                    [SystemMessage(system), HumanMessage(human)]
                )
        except Exception as exc:  # invalid structured output, provider error, ...
            if getattr(exc, "status_code", None) == RATE_LIMITED and attempt < retries:
                logger.warning("%s call was rate limited; retrying in %ss", name, retry_wait)
                await asyncio.sleep(retry_wait)
                continue
            logger.exception("%s call failed", name)
            return None
        if isinstance(result, schema):
            return result
        logger.warning("%s call returned no usable output: %r", name, result)
        return None
    return None
