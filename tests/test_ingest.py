"""POST /projects/{id}/ingest: auth, limits, dedup and what gets stored."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DataError, OperationalError

from app.core.config import Settings
from app.ingestion.sources import PushSource
from app.models import LogEvent
from tests.project_helpers import make_project, projects_client

pytestmark = pytest.mark.integration

TOKEN = "s3cret"
URL = "/api/v1/projects/demo/ingest"
WSO2_ERROR = "TID: [-1234] [] [2026-11-04 10:00:00,123] ERROR {org.acme.Pool} - pool exhausted"


@pytest.fixture(autouse=True)
def token_env(monkeypatch):
    monkeypatch.setenv("DEMO_TOKEN", TOKEN)


def push_project(timezone: str = "UTC", **kwargs):
    source = PushSource(id="demo/push", token_env="DEMO_TOKEN", timezone=timezone)
    return make_project(timezone=timezone, push_source=source, **kwargs)


async def push(http, body, *, token: str | None = TOKEN, content_type: str = "application/json", url: str = URL):
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {"Content-Type": content_type, **({"Authorization": f"Bearer {token}"} if token else {})}
    return await http.post(url, content=content, headers=headers)


async def rows(factory) -> list[LogEvent]:
    async with factory() as s:
        return list((await s.scalars(select(LogEvent).order_by(LogEvent.created_at, LogEvent.event_key))).all())


async def test_push_stores_events(session_factory):
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, [{"message": "first"}, {"message": "second"}])
    assert res.status_code == 200
    assert res.json() == {"received": 2, "stored": 2, "duplicates": 0}
    stored = await rows(session_factory)
    assert {r.source_id for r in stored} == {"demo/push"}
    assert all(r.embedding is None and r.locator == {"kind": "push"} for r in stored)


async def test_same_batch_twice_is_deduplicated(session_factory):
    batch = [{"message": "first"}, {"message": "second"}]
    async with projects_client(session_factory, (push_project(),)) as http:
        await push(http, batch)
        res = await push(http, batch)
    assert res.json() == {"received": 2, "stored": 0, "duplicates": 2}
    assert len(await rows(session_factory)) == 2


async def test_duplicate_inside_one_batch(session_factory):
    event = {"message": "same"}
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, [event, event])
    assert res.json() == {"received": 2, "stored": 1, "duplicates": 1}


async def test_secret_is_redacted_and_deduplicates(session_factory):
    batch = [{"message": "login failed password=hunter2"}, {"message": "login failed password=other"}]
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, batch)
    assert res.json()["stored"] == 1
    (stored,) = await rows(session_factory)
    assert "hunter2" not in stored.raw_text and "other" not in stored.raw_text


@pytest.mark.parametrize("token", [None, "wrong"])
async def test_missing_or_wrong_token_is_401(session_factory, token):
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, [{"message": "x"}], token=token)
    assert res.status_code == 401
    assert await rows(session_factory) == []


async def test_wrong_scheme_is_401(session_factory):
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await http.post(URL, json=[{"message": "x"}], headers={"Authorization": f"Basic {TOKEN}"})
    assert res.status_code == 401


async def test_not_found_cases_look_the_same(session_factory, monkeypatch):
    async with projects_client(session_factory, (push_project(), make_project("plain"))) as http:
        unknown = await push(http, [{"message": "x"}], url="/api/v1/projects/nope/ingest")
        no_push = await push(http, [{"message": "x"}], url="/api/v1/projects/plain/ingest")
        monkeypatch.delenv("DEMO_TOKEN")
        unset = await push(http, [{"message": "x"}])
    assert unknown.status_code == no_push.status_code == unset.status_code == 404
    assert unknown.json() == no_push.json() == unset.json()


async def test_invalid_project_is_409(session_factory):
    async with projects_client(session_factory, (push_project(error="bad yaml"),)) as http:
        res = await push(http, [{"message": "x"}])
    assert res.status_code == 409


async def test_too_many_events_is_413(session_factory):
    async with projects_client(session_factory, (push_project(),)) as http:
        ok = await push(http, [{"message": f"m{i}"} for i in range(1000)])
        res = await push(http, [{"message": f"m{i}"} for i in range(1001)])
    assert ok.status_code == 200 and res.status_code == 413


async def test_oversize_body_is_413(session_factory):
    body = b'[{"message": "' + b"a" * (1024 * 1024) + b'"}]'
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, body)
    assert res.status_code == 413
    assert await rows(session_factory) == []


@pytest.mark.parametrize("body", [
    b"{", b"\xff\xfe", b'{"message": "x"}', b'[{"level": "x"}]', b'[{"message": ""}]', b'[{"message": "   "}]',
    b'["just text"]',
])
async def test_malformed_bodies_are_422(session_factory, body):
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, body)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "validation_error"
    assert await rows(session_factory) == []


async def test_ndjson_body(session_factory):
    ndjson = b'{"message": "one"}\n{"message": "two"}\n'
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, ndjson, content_type="application/x-ndjson")
        bad = await push(http, b'{"message": "one"}\nnot json\n', content_type="application/x-ndjson")
    assert res.json()["stored"] == 2
    assert bad.status_code == 422


async def test_timestamps(session_factory):
    events = [
        {"message": "utc", "timestamp": "2026-11-04T10:00:00Z"},
        {"message": "naive", "timestamp": "2026-11-04T10:00:00"},
        {"message": "garbage", "timestamp": "not a time"},
    ]
    async with projects_client(session_factory, (push_project("Asia/Kolkata"),)) as http:
        res = await push(http, events)
    assert res.status_code == 200
    by_text = {r.raw_text: r.occurred_at for r in await rows(session_factory)}
    assert by_text["utc"] == datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    assert by_text["naive"] == datetime(2026, 11, 4, 4, 30, tzinfo=UTC)


async def test_sender_level_used_only_when_parser_finds_none(session_factory):
    events = [{"message": "plain text", "level": "warn"}, {"message": WSO2_ERROR, "level": "info"}]
    async with projects_client(session_factory, (push_project(),)) as http:
        await push(http, events)
    by_text = {r.raw_text: r.level for r in await rows(session_factory)}
    assert by_text["plain text"] == "WARN"
    assert by_text[WSO2_ERROR] == "ERROR"


async def test_database_down_is_503(session_factory):
    from app.core.database import get_session

    class BrokenSession:
        def begin(self):
            raise OperationalError("insert", {}, Exception("connection refused"))

    async def broken():
        yield BrokenSession()

    async with projects_client(session_factory, (push_project(),), {get_session: broken}) as http:
        res = await push(http, [{"message": "secret-looking text"}])
    assert res.status_code == 503
    assert "secret-looking" not in res.text and "connection refused" not in res.text


async def test_concurrent_identical_pushes_store_one_row(session_factory):
    async with projects_client(session_factory, (push_project(),)) as http:
        results = await asyncio.gather(*(push(http, [{"message": "race"}]) for _ in range(2)))
    assert sorted(r.json()["stored"] for r in results) == [0, 1]
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(LogEvent)) == 1


# --- fixes from the final review ---


async def test_token_set_only_in_dotenv_settings_is_found(session_factory, monkeypatch):
    monkeypatch.delenv("DEMO_TOKEN")  # .env values reach Settings.model_extra, never os.environ
    settings = Settings(_env_file=None, database_url="postgresql://u@h/db", DEMO_TOKEN=TOKEN)
    monkeypatch.setattr("app.ingestion.push.get_settings", lambda: settings)
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, [{"message": "x"}])
    assert res.status_code == 200


async def test_timestamps_at_the_edge_of_the_range_are_not_a_500(session_factory):
    events = [{"message": "min", "timestamp": "0001-01-01T00:00:00"}, {"message": "max", "timestamp": "9999-12-31T23:59:59"}]
    async with projects_client(session_factory, (push_project("Asia/Kolkata"),)) as http:
        res = await push(http, events)
    assert res.status_code == 200 and res.json()["stored"] == 2


@pytest.mark.parametrize("message", ["\u0000\u0000", "[31m", "\u200b"])
async def test_message_that_sanitizes_to_nothing_is_422(session_factory, message):
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, [{"message": message}])
    assert res.status_code == 422
    assert await rows(session_factory) == []


async def test_long_message_is_clipped(session_factory):
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, [{"message": "a" * 900_000}])
    assert res.status_code == 200
    (stored,) = await rows(session_factory)
    assert len(stored.raw_text) == 256 * 1024


async def test_unknown_sender_level_is_dropped(session_factory):
    events = [
        {"message": "one", "level": "banana!!"}, {"message": "two", "level": "ERR\u0000"},
        {"message": "three", "level": " warning "},
    ]
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, events)
    assert res.status_code == 200
    by_text = {r.raw_text: r.level for r in await rows(session_factory)}
    assert by_text == {"one": None, "two": None, "three": "WARNING"}


async def test_a_database_error_that_is_not_a_connection_problem_is_not_503(session_factory):
    from app.core.database import get_session

    class BuggySession:
        def begin(self):
            raise DataError("insert", {}, Exception("bad value"))

    async def buggy():
        yield BuggySession()

    async with projects_client(session_factory, (push_project(),), {get_session: buggy}) as http:
        with pytest.raises(DataError):
            await push(http, [{"message": "x"}])


async def test_invalid_project_without_a_token_is_401_and_explains_nothing(session_factory):
    async with projects_client(session_factory, (push_project(error="push_token_env must match"),)) as http:
        res = await push(http, [{"message": "x"}], token=None)
    assert res.status_code == 401
    assert "push_token_env" not in res.text
