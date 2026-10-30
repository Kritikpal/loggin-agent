"""LangChain configuration: builds the Groq chat model and rotates through the configured API keys."""

import itertools

from langchain_core.language_models import BaseChatModel
from langchain_groq import ChatGroq

from app.core.config import get_settings
from app.core.errors import AIUnavailableError

# ponytail: round-robin per model built, in this process. Groq limits each organisation, not each key, so extra
# keys only help across organisations. Upgrade path if 429s persist: on a 429, retry the call on the next key.
_turns = itertools.count()


def chat_model() -> BaseChatModel:
    settings = get_settings()
    keys = settings.groq_api_keys
    if not keys:
        raise AIUnavailableError("The AI assistant is not configured (GROQ_API_KEY is missing).")
    return ChatGroq(
        model=settings.groq_model, api_key=keys[next(_turns) % len(keys)], temperature=0, timeout=30, max_retries=1
    )
