"""POST /projects/{id}/ingest: auth, limits, dedup and what gets stored."""

import asyncio
import json
import re
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
    assert res.json() == {"received": 2, "stored": 2, "duplicates": 0, "skipped": 0}
    stored = await rows(session_factory)
    assert {r.source_id for r in stored} == {"demo/push"}
    assert all(r.embedding is None and r.locator == {"kind": "push"} for r in stored)


async def test_same_batch_twice_is_deduplicated(session_factory):
    batch = [{"message": "first"}, {"message": "second"}]
    async with projects_client(session_factory, (push_project(),)) as http:
        await push(http, batch)
        res = await push(http, batch)
    assert res.json() == {"received": 2, "stored": 0, "duplicates": 2, "skipped": 0}
    assert len(await rows(session_factory)) == 2


async def test_duplicate_inside_one_batch(session_factory):
    event = {"message": "same"}
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, [event, event])
    assert res.json() == {"received": 2, "stored": 1, "duplicates": 1, "skipped": 0}


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


async def test_more_events_than_the_configured_limit_is_413(session_factory, monkeypatch):
    settings = Settings(_env_file=None, database_url="postgresql://u@h/db", push_max_events=3)
    monkeypatch.setattr("app.ingestion.push.get_settings", lambda: settings)
    async with projects_client(session_factory, (push_project(),)) as http:
        ok = await push(http, [{"message": f"m{i}"} for i in range(3)])
        res = await push(http, [{"message": f"m{i}"} for i in range(4)])
    assert ok.status_code == 200 and res.status_code == 413
    assert "At most 3 events" in res.json()["error"]["message"]


async def test_a_batch_of_fluent_bit_size_is_accepted_by_default(session_factory):
    events = [{"message": f"event number {i}"} for i in range(5000)]  # far above the old 1000
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, events)
    assert res.status_code == 200 and res.json()["stored"] == 5000


def with_body_limit(monkeypatch, limit: int) -> None:
    settings = Settings(_env_file=None, database_url="postgresql://u@h/db", push_max_body_bytes=limit)
    monkeypatch.setattr("app.ingestion.push.get_settings", lambda: settings)


async def test_body_over_the_configured_limit_is_413(session_factory, monkeypatch):
    with_body_limit(monkeypatch, 2048)
    body = b'[{"message": "' + b"a" * 3000 + b'"}]'
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, body)
    assert res.status_code == 413
    assert await rows(session_factory) == []


async def test_a_body_over_one_mib_is_accepted_by_default(session_factory):
    events = [{"message": f"{i} " + "a" * 300_000} for i in range(5)]  # about 1.5 MiB, each message under its own cap
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, events)
    assert res.status_code == 200 and res.json()["stored"] == 5


@pytest.mark.parametrize("body", [
    b"{", b"\xff\xfe", b'{"message": "x"}', b'[{"level": "x"}]', b'["just text"]',
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


BLANKS = ["", "   ", "\n\t", "\u0000\u0000", "\x1b[31m", "\u200b"]  # empty, whitespace, control characters only


@pytest.mark.parametrize("blank", BLANKS)
async def test_a_blank_message_is_skipped_and_the_rest_of_the_batch_is_stored(session_factory, blank):
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, [{"message": blank}, {"message": "real event"}])
    assert res.status_code == 200
    assert res.json() == {"received": 2, "stored": 1, "duplicates": 0, "skipped": 1}
    assert [r.raw_text for r in await rows(session_factory)] == ["real event"]


async def test_blank_messages_log_one_warning_per_request_without_content(session_factory, caplog):
    batch = [{"message": blank} for blank in BLANKS] + [{"message": "real event"}]
    with caplog.at_level("WARNING", logger="app.controllers.ingest"):
        async with projects_client(session_factory, (push_project(),)) as http:
            await push(http, batch)
    warnings = [r.getMessage() for r in caplog.records if "blank" in r.getMessage()]
    assert len(warnings) == 1
    assert f"{len(BLANKS)} blank" in warnings[0] and "demo/push" in warnings[0]


async def test_a_batch_of_only_blank_messages_is_ok_and_stores_nothing(session_factory):
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, [{"message": ""}, {"message": " "}])
    assert res.json() == {"received": 2, "stored": 0, "duplicates": 0, "skipped": 2}
    assert await rows(session_factory) == []


async def test_a_batch_without_blank_messages_logs_no_warning(session_factory, caplog):
    with caplog.at_level("WARNING", logger="app.controllers.ingest"):
        async with projects_client(session_factory, (push_project(),)) as http:
            await push(http, [{"message": "real event"}])
    assert not [r for r in caplog.records if "blank" in r.getMessage()]


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


def test_openapi_documents_the_body_and_the_token_header():
    from app.main import create_app

    op = create_app().openapi()["paths"]["/api/v1/projects/{project_id}/ingest"]["post"]
    body = op["requestBody"]
    assert body["required"] is True
    assert body["content"]["application/json"]["schema"]["items"]["required"] == ["message"]
    assert "application/x-ndjson" in body["content"]
    assert "authorization" in [p["name"] for p in op["parameters"]]


# --- audit fields and e-mail pseudonyms ---

AUDIT_LOGIN = (
    "TID: [-1234] [] [2026-11-05 16:03:26,686] [f39cb36f]  INFO {{AUDIT_LOG}} - Initiator : {who} | Action : Login | "
    'Target : ApplicationAuthenticationFramework | Data : {{ "AuthenticatedUser" : "{who}",'
    '"ServiceProviderName" : "Console" }} | Result : Success'
)
CODED = r"\[REDACTED:email:u[0-9a-f]{11}\]"


def use_pseudonym_key(monkeypatch, key: bytes | None) -> None:
    monkeypatch.setattr("app.ingestion.pipeline.configured_pseudonym_key", lambda: key)
    monkeypatch.setattr("app.ingestion.push.configured_pseudonym_key", lambda: key)


async def test_pushed_audit_line_stores_audit_fields_with_a_coded_user(session_factory, monkeypatch):
    use_pseudonym_key(monkeypatch, b"test-key")
    async with projects_client(session_factory, (push_project(),)) as http:
        res = await push(http, [{"message": AUDIT_LOGIN.format(who="a@x.com")}])
    assert res.json()["stored"] == 1
    (stored,) = await rows(session_factory)
    assert "a@x.com" not in stored.raw_text
    fields = stored.fields
    assert fields["audit_actor"] == fields["audit_user"]
    assert re.fullmatch(CODED, fields["audit_user"])
    assert (fields["audit_action"], fields["audit_application"], fields["audit_result"]) == ("Login", "Console", "Success")


async def test_two_users_have_one_signature_and_two_codes(session_factory, monkeypatch):
    use_pseudonym_key(monkeypatch, b"test-key")
    batch = [{"message": AUDIT_LOGIN.format(who=who)} for who in ("a@x.com", "b@x.com")]
    async with projects_client(session_factory, (push_project(),)) as http:
        await push(http, batch)
    first, second = await rows(session_factory)
    assert first.signature == second.signature
    assert first.fields["audit_user"] != second.fields["audit_user"]


async def test_the_same_person_keeps_one_code_across_pushes(session_factory, monkeypatch):
    use_pseudonym_key(monkeypatch, b"test-key")
    batch = [{"message": AUDIT_LOGIN.format(who="Bob@X.com"), "timestamp": "2026-11-05T10:00:00Z"}]
    again = [{"message": AUDIT_LOGIN.format(who="bob@x.com"), "timestamp": "2026-11-05T11:00:00Z"}]
    async with projects_client(session_factory, (push_project(),)) as http:
        await push(http, batch)
        await push(http, again)
    first, second = await rows(session_factory)
    assert first.fields["audit_user"] == second.fields["audit_user"]


async def test_without_a_key_the_plain_mask_is_stored(session_factory, monkeypatch):
    use_pseudonym_key(monkeypatch, None)
    async with projects_client(session_factory, (push_project(),)) as http:
        await push(http, [{"message": AUDIT_LOGIN.format(who="a@x.com")}])
    (stored,) = await rows(session_factory)
    assert stored.fields["audit_user"] == "[REDACTED:email]" and "a@x.com" not in stored.raw_text


@pytest.mark.parametrize(("key", "warnings"), [(None, 1), (b"k", 0)])
async def test_lifespan_warns_once_when_the_key_is_missing(monkeypatch, caplog, key, warnings):
    from app import main

    async def idle(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "run_embed_loop", idle)
    monkeypatch.setattr(main, "configured_pseudonym_key", lambda: key)
    with caplog.at_level("WARNING", logger="app.main"):
        async with main.lifespan(main.app):
            pass
    assert len([r for r in caplog.records if "PSEUDONYM_KEY" in r.getMessage()]) == warnings
    assert b"k" not in caplog.text.encode() or key is None  # the key itself is never logged
