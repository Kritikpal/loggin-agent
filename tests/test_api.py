import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app
from tests.search_helpers import api_client


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as http:
        yield http


async def test_health(client):
    assert (await client.get("/api/v1/health")).json() == {"status": "ok"}


@pytest.mark.integration
async def test_search_logs_with_no_sources_configured_finds_nothing(session_factory):
    body = {"query": "Why are database connections failing?", "levels": ["error", "warn"], "top_k": 5}
    async with api_client(session_factory, []) as http:
        res = await http.post("/api/v1/tools/search-logs", json=body)
    assert res.status_code == 200 and res.json() == {"results": [], "coverage": []}


@pytest.mark.parametrize("body", [
    {},
    {"query": ""},
    {"query": "x", "top_k": 0},
    {"query": "x", "start_time": "2026-10-02T00:00:00Z", "end_time": "2026-10-01T00:00:00Z"},
    {"query": "x", "unknown": 1},
])
async def test_search_logs_rejects_invalid_requests(client, body):
    assert (await client.post("/api/v1/tools/search-logs", json=body)).status_code == 422


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
