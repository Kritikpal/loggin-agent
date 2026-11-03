"""The read-only projects API and project-scoped search (spec 2026-11-03 §5)."""

from pathlib import Path

import pytest

from app.ingestion.projects import scan_projects
from app.ingestion.sources import FileSource
from tests.project_helpers import SMALL_WSO2_LOG, make_project, make_project_folder, projects_client
from tests.search_helpers import index_file

pytestmark = pytest.mark.integration

STEM = Path(SMALL_WSO2_LOG).stem


async def test_list_shows_ok_and_invalid_projects(tmp_path, session_factory):
    make_project_folder(tmp_path, "good", "title: Good\ntype: wso2\n", SMALL_WSO2_LOG)
    make_project_folder(tmp_path, "broken", "type: nope\n")

    async with projects_client(session_factory, lambda: scan_projects(tmp_path)) as http:
        body = (await http.get("/api/v1/projects")).json()

    broken, good = body
    assert broken["id"] == "broken" and broken["status"] == "invalid" and broken["error"]
    assert good == {"id": "good", "title": "Good", "type": "wso2", "status": "ok", "file_count": 1,
                    "indexed_from": None, "indexed_to": None, "error": None}


async def test_detail_has_description_timezone_and_include(session_factory):
    project = make_project("p", description="Fronts the payments API", timezone="Asia/Kolkata")
    async with projects_client(session_factory, (project,)) as http:
        body = (await http.get("/api/v1/projects/p")).json()
    assert body["description"] == "Fronts the payments API"
    assert body["timezone"] == "Asia/Kolkata" and body["include"] == "*.log*"


@pytest.mark.parametrize("path", ["UNKNOWN", "no-such-project", "p.", "-p"])
async def test_unknown_and_malformed_ids_share_one_404(session_factory, path):
    async with projects_client(session_factory, (make_project("p"),)) as http:
        for suffix in ("", "/files"):
            res = await http.get(f"/api/v1/projects/{path}{suffix}")
            assert res.status_code == 404
            assert res.json()["error"]["message"] == "No such project."


@pytest.mark.parametrize("path", ["%2E%2E%2Fetc", "p%2Fx", "..%2F..%2Fetc%2Fpasswd"])
async def test_encoded_slashes_never_reach_a_project(session_factory, path):
    """A decoded '/' splits the path, so no route matches: the router's own 404, never a file or a project."""
    async with projects_client(session_factory, (make_project("p"),)) as http:
        res = await http.get(f"/api/v1/projects/{path}/files")
    assert res.status_code == 404 and "p" not in res.json()["error"]["message"].split()


async def test_files_lists_indexed_skipped_and_missing(tmp_path, session_factory):
    folder = make_project_folder(tmp_path, "p", "title: P\ntype: wso2\ntimezone: Asia/Kolkata\n", SMALL_WSO2_LOG)
    (folder / "bad name.log").write_text("x\n", encoding="utf-8")
    [project] = scan_projects(tmp_path)
    await index_file(project.files[0], session_factory)
    await index_file(FileSource(id="p/gone", path=tmp_path / "nowhere.log"), session_factory)  # records an error row

    async with projects_client(session_factory, lambda: scan_projects(tmp_path)) as http:
        files = (await http.get("/api/v1/projects/p/files")).json()

    assert [(f["id"], f["status"]) for f in files] == [(STEM, "complete"), ("bad name.log", "skipped"), ("gone", "missing")]
    indexed, skipped, missing = files
    assert indexed["name"] == SMALL_WSO2_LOG and indexed["event_count"] > 0 and indexed["size_bytes"] > 0
    assert 0 < indexed["parsed_ratio"] <= 1 and indexed["first_at"] and indexed["last_at"]
    assert "name" in skipped["last_error"] and skipped["event_count"] == 0
    assert missing["name"] == "gone" and missing["size_bytes"] is None


async def test_the_same_file_name_in_two_projects_is_kept_apart(tmp_path, session_factory):
    for project_id in ("a", "b"):
        make_project_folder(tmp_path, project_id, "title: X\ntype: wso2\n", SMALL_WSO2_LOG)
    for project in scan_projects(tmp_path):
        await index_file(project.files[0], session_factory)

    async with projects_client(session_factory, lambda: scan_projects(tmp_path)) as http:
        counts = [(await http.get(f"/api/v1/projects/{p}/files")).json()[0]["event_count"] for p in ("a", "b")]
        listed = (await http.get("/api/v1/projects")).json()
        res = await http.post("/api/v1/projects/a/search-logs", json={"query": "error", "top_k": 50})
        other = await http.post("/api/v1/projects/a/search-logs", json={"query": "error", "source_ids": [f"b/{STEM}"]})

    assert counts[0] == counts[1] > 0
    assert all(p["indexed_from"] and p["indexed_to"] for p in listed)
    assert res.status_code == 200 and res.json()["results"]
    assert {hit["source_id"] for hit in res.json()["results"]} == {f"a/{STEM}"}
    assert other.status_code == 404


async def test_search_on_an_invalid_project_is_refused(session_factory):
    async with projects_client(session_factory, (make_project("p", error="bad yaml"),)) as http:
        res = await http.post("/api/v1/projects/p/search-logs", json={"query": "x"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "project_invalid"


async def test_old_routes_are_gone(session_factory):
    async with projects_client(session_factory, ()) as http:
        assert (await http.get("/api/v1/sources")).status_code == 404
        assert (await http.post("/api/v1/tools/search-logs", json={"query": "x"})).status_code == 404
