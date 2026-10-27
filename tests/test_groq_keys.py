"""Several Groq API keys (GROQ_API_KEY, GROQ_API_KEY_1, _2, ...) are handed out round-robin, one per model built,
so the main model, the fast model and the background memory extraction spread their calls over the keys."""

import pytest
from langchain_core.messages import AIMessage

from app.ai import conversation
from app.ai.llm import chat_model, fast_chat_model
from app.core.config import Settings, get_settings
from tests.scripted_model import ScriptedModel, extraction

URL = "/api/v1/ai/chat"


def key_of(model) -> str:
    return model.groq_api_key.get_secret_value()


def test_keys_are_collected_in_number_order_without_blanks_or_repeats():
    settings = Settings(
        _env_file=None, database_url="postgresql://u@h/db", groq_api_key="k0",
        GROQ_API_KEY_10="k10", groq_api_key_2="k2", GROQ_API_KEY_1="k1", groq_api_key_3=" ", groq_api_key_4="k1",
    )
    assert settings.groq_api_keys == ["k0", "k1", "k2", "k10"]


def test_numbered_keys_work_without_the_plain_one():
    assert Settings(_env_file=None, database_url="postgresql://u@h/db", GROQ_API_KEY_1="k1").groq_api_keys == ["k1"]


def test_models_take_the_keys_in_turn(monkeypatch):
    monkeypatch.setattr(get_settings(), "groq_api_keys", ["k0", "k1", "k2"])

    keys = [key_of(chat_model()) for _ in range(6)]

    assert sorted(keys[:3]) == ["k0", "k1", "k2"] and keys[3:] == keys[:3]  # every key once, then the same order


def test_the_fast_model_takes_the_next_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "groq_api_keys", ["k0", "k1"])
    monkeypatch.setattr(get_settings(), "groq_fast_model", "fast-model")

    main = chat_model()
    fast = fast_chat_model(main)

    assert fast.model_name == "fast-model" and key_of(fast) != key_of(main)


def test_one_key_is_used_for_everything(monkeypatch):
    monkeypatch.setattr(get_settings(), "groq_api_keys", ["only"])
    assert {key_of(chat_model()) for _ in range(3)} == {"only"}


@pytest.mark.usefixtures("tool_session", "checkpointer")
async def test_memory_extraction_gets_its_own_model(client, session_factory, monkeypatch):
    """The extraction runs after the reply; built separately, it gets the next key instead of the agent's."""
    from app.api.v1.ai import memory_sessions

    agent = ScriptedModel(script=[AIMessage("Noted.")], seen=[])
    memory = ScriptedModel(script=[extraction()], seen=[])
    built = [agent, memory]
    app = client._transport.app
    app.dependency_overrides[chat_model] = lambda: built.pop(0)
    app.dependency_overrides[memory_sessions] = lambda: session_factory
    given = []

    async def spy(model, *args, **kwargs):
        given.append(model)

    monkeypatch.setattr(conversation, "remember", spy)

    res = await client.post(URL, json={"message": "Always give me weekly reports, Monday to Sunday."})

    assert res.status_code == 200 and given == [memory] and len(agent.seen) == 1
