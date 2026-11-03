import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as http:
        yield http


async def test_health(client):
    assert (await client.get("/api/v1/health")).json() == {"status": "ok"}


async def test_memories_and_resume_are_mocks(client):
    assert len((await client.get("/api/v1/ai/memories")).json()) == 2
    res = await client.post(f"/api/v1/ai/workflows/{uuid.uuid4()}/resume", json={"decision": "REJECTED"})
    assert res.json()["status"] == "REJECTED"


def test_groq_keys_are_collected_in_number_order_without_blanks_or_repeats():
    settings = Settings(
        _env_file=None, database_url="postgresql://u@h/db", groq_api_key="k0",
        GROQ_API_KEY_10="k10", groq_api_key_2="k2", GROQ_API_KEY_1="k1", groq_api_key_3=" ", groq_api_key_4="k1",
    )
    assert settings.groq_api_keys == ["k0", "k1", "k2", "k10"]
