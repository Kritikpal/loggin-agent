"""Projects and their logs live in the database only: create, edit and delete projects over the API, push or upload
logs with the project's own token, and nothing is read from disk."""

import json

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.ingestion.projects import hash_token, project_from_config
from app.ingestion.sources import ConfigError
from app.models import LogEvent, SourceIndexState
from app.models.project import ProjectRow
from tests.project_helpers import db_client

pytestmark = pytest.mark.integration

ADMIN = {"Authorization": "Bearer admin-key"}
NEW = {"id": "shop", "title": "Shop", "type": "wso2", "timezone": "Asia/Kolkata"}
LOG = (
    "TID: [-1234] [] [2026-11-04 10:00:00,123] ERROR {org.acme.Pool} - pool exhausted\n"
    "java.lang.IllegalStateException: no connections\n"
    "\tat org.acme.Pool.get(Pool.java:10)\n"
    "TID: [-1234] [] [2026-11-04 10:00:05,000]  INFO {org.acme.Pool} - recovered\n"
)


@pytest.fixture(autouse=True)
def admin_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_api_key", "admin-key")


async def create(http, body=NEW):
    return await http.post("/api/v1/projects", json=body, headers=ADMIN)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_create_needs_the_admin_key(session_factory, monkeypatch):
    async with db_client(session_factory) as http:
        assert (await http.post("/api/v1/projects", json=NEW)).status_code == 401
        assert (await http.post("/api/v1/projects", json=NEW, headers=bearer("wrong"))).status_code == 401
        monkeypatch.setattr(get_settings(), "admin_api_key", None)  # not configured: writes stay closed
        assert (await create(http)).status_code == 401


async def test_create_returns_the_token_once_and_stores_only_its_hash(session_factory):
    async with db_client(session_factory) as http:
        res = await create(http)
        listed = (await http.get("/api/v1/projects")).json()
        detail = (await http.get("/api/v1/projects/shop")).json()
    assert res.status_code == 201
    body = res.json()
    token = body.pop("push_token")
    assert body == detail and detail["title"] == "Shop" and detail["timezone"] == "Asia/Kolkata"
    assert [p["id"] for p in listed] == ["shop"] and "push_token" not in listed[0]
    async with session_factory() as s:
        row = await s.get(ProjectRow, "shop")
    assert row.push_token_hash == hash_token(token) and token not in json.dumps(row.config)


@pytest.mark.parametrize("change", [
    {"id": "Bad Id"}, {"type": "nope"}, {"timezone": "Mars/Base"}, {"title": " "}, {"event_start": "(unclosed"}])
async def test_create_rejects_bad_input(session_factory, change):
    async with db_client(session_factory) as http:
        res = await create(http, {**NEW, **change})
    assert res.status_code == 422


async def test_duplicate_id_is_a_conflict(session_factory):
    async with db_client(session_factory) as http:
        await create(http)
        assert (await create(http)).status_code == 409


async def test_push_and_upload_use_the_projects_own_token(session_factory):
    async with db_client(session_factory) as http:
        token = (await create(http)).json()["push_token"]
        pushed = await http.post("/api/v1/projects/shop/ingest", json=[{"message": "hello"}], headers=bearer(token))
        denied = await http.post("/api/v1/projects/shop/ingest", json=[{"message": "x"}], headers=bearer("nope"))
        uploaded = await http.post("/api/v1/projects/shop/upload", content=LOG.encode(), headers=bearer(token))
        again = await http.post("/api/v1/projects/shop/upload", content=LOG.encode(), headers=bearer(token))
        no_token = await http.post("/api/v1/projects/shop/upload", content=LOG.encode())
    assert pushed.json()["stored"] == 1 and denied.status_code == 401 and no_token.status_code == 401
    assert uploaded.json() == {"received": 2, "stored": 2, "duplicates": 0, "skipped": 0}  # the trace is ONE event
    assert again.json() == {"received": 2, "stored": 0, "duplicates": 2, "skipped": 0}
    async with session_factory() as s:
        sources = set(await s.scalars(select(LogEvent.source_id)))
        error = await s.scalar(select(LogEvent).where(LogEvent.level == "ERROR"))
    assert sources == {"shop/push"} and "IllegalStateException" in error.raw_text


async def test_upload_rejects_a_body_that_is_not_text(session_factory):
    async with db_client(session_factory) as http:
        token = (await create(http)).json()["push_token"]
        res = await http.post("/api/v1/projects/shop/upload", content=b"\xff\xfe\x00bad", headers=bearer(token))
    assert res.status_code == 422


async def test_files_endpoint_shows_the_pushed_logs(session_factory):
    async with db_client(session_factory) as http:
        token = (await create(http)).json()["push_token"]
        await http.post("/api/v1/projects/shop/upload", content=LOG.encode(), headers=bearer(token))
        files = (await http.get("/api/v1/projects/shop/files")).json()
    assert [(f["id"], f["status"], f["event_count"]) for f in files] == [("push", "complete", 2)]


async def test_patch_edits_fields_and_config(session_factory):
    async with db_client(session_factory) as http:
        await create(http, {**NEW, "app_packages": ["com.acme"]})
        res = await http.patch("/api/v1/projects/shop", json={"title": "Shop 2", "app_packages": None}, headers=ADMIN)
        bad = await http.patch("/api/v1/projects/shop", json={"type": "nope"}, headers=ADMIN)
        missing = await http.patch("/api/v1/projects/ghost", json={"title": "x"}, headers=ADMIN)
        open_ = await http.patch("/api/v1/projects/shop", json={"title": "x"})
    assert res.status_code == 200 and res.json()["title"] == "Shop 2"
    assert bad.status_code == 422 and missing.status_code == 404 and open_.status_code == 401
    async with session_factory() as s:
        assert "app_packages" not in (await s.get(ProjectRow, "shop")).config


async def test_rotating_the_token_replaces_the_old_one(session_factory):
    async with db_client(session_factory) as http:
        old = (await create(http)).json()["push_token"]
        new = (await http.post("/api/v1/projects/shop/token", headers=ADMIN)).json()["push_token"]
        url = "/api/v1/projects/shop/ingest"
        old_res = await http.post(url, json=[{"message": "a"}], headers=bearer(old))
        new_res = await http.post(url, json=[{"message": "a"}], headers=bearer(new))
    assert old_res.status_code == 401 and new_res.status_code == 200


async def test_delete_removes_the_project_and_only_its_logs(session_factory):
    async with db_client(session_factory) as http:
        shop = (await create(http)).json()["push_token"]
        other = (await create(http, {**NEW, "id": "shop_2"})).json()["push_token"]  # '_' is not a LIKE wildcard
        for project, token in (("shop", shop), ("shop_2", other)):
            await http.post(f"/api/v1/projects/{project}/upload", content=LOG.encode(), headers=bearer(token))
        async with session_factory() as s, s.begin():
            s.add(SourceIndexState(source_id="shop/push", last_error=None))
        denied = await http.delete("/api/v1/projects/shop")
        res = await http.delete("/api/v1/projects/shop", headers=ADMIN)
        gone = await http.get("/api/v1/projects/shop")
    assert denied.status_code == 401 and res.status_code == 204 and gone.status_code == 404
    async with session_factory() as s:
        assert set(await s.scalars(select(LogEvent.source_id))) == {"shop_2/push"}
        assert await s.scalar(select(func.count()).select_from(SourceIndexState)) == 0


def test_a_database_project_has_one_push_source_and_no_files():
    project = project_from_config(
        "shop", {**NEW, "description": "d", "app_packages": ["com.acme"]}, token_hash="h")
    assert project.files == () and project.folder is None
    assert project.push_source.id == "shop/push" and project.push_source.token_hash == "h"
    assert project.push_source.app_packages == ("com.acme",)


def test_a_bad_config_is_rejected():
    with pytest.raises(ConfigError):
        project_from_config("shop", {**NEW, "type": "nope"}, token_hash="h")


async def test_import_moves_a_folder_project_into_the_database(tmp_path, session_factory):
    from app.models import LogEvent
    from scripts.import_projects import run
    from tests.project_helpers import SMALL_WSO2_LOG, make_project_folder

    make_project_folder(tmp_path, "old", "title: Old\ntype: wso2\ntimezone: Asia/Kolkata\n", SMALL_WSO2_LOG)
    assert await run(tmp_path, None, session_factory) == 0
    assert await run(tmp_path, None, session_factory) == 0  # a second run skips it
    async with session_factory() as s:
        row = await s.get(ProjectRow, "old")
        sources = set(await s.scalars(select(LogEvent.source_id)))
        events = await s.scalar(select(func.count()).select_from(LogEvent))
    assert row.title == "Old" and row.timezone == "Asia/Kolkata"
    assert sources == {"old/push"} and events > 10
