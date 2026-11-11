"""Push sources: a project's pushed logs, next to its file sources."""

import pytest

from app.ingestion.projects import scan_projects
from tests.project_helpers import WSO2_YAML, make_project_folder


def only_project(tmp_path):
    (project,) = scan_projects(tmp_path)
    return project


def test_project_without_push_has_no_push_source(tmp_path):
    make_project_folder(tmp_path, "demo")
    project = only_project(tmp_path)
    assert project.push_source is None
    assert project.sources == project.files


# --- the push source is visible to coverage, the files listing and the agent prompt ---

from datetime import UTC, datetime

from app.agent.prompt import build_system_prompt
from app.ingestion.coverage import coverage_for_all, orphan_ids
from app.ingestion.sources import PushSource
from app.ingestion.store import EventRow, advance_state, upsert_events
from app.ingestion.readers.base import Cursor
from tests.project_helpers import make_project, projects_client

PUSH = PushSource(id="demo/push")


def push_row(key: str = "k1") -> EventRow:
    return EventRow(
        event_key=key, locator={"kind": "push"}, occurred_at=datetime(2026, 11, 4, tzinfo=UTC), level="ERROR",
        raw_text="ERROR boom", fields={}, semantic_text="ERROR boom")


@pytest.mark.integration
async def test_coverage_of_push_source_is_complete(session):
    (coverage,) = await coverage_for_all(session, [PUSH])
    assert (coverage.source_id, coverage.status) == ("demo/push", "complete")


@pytest.mark.integration
async def test_push_id_is_not_an_orphan(session_factory):
    async with session_factory() as s, s.begin():
        await advance_state(s, "demo/push", Cursor(), None, None)
    async with session_factory() as s:
        assert await orphan_ids(s, make_project(push_source=PUSH)) == []


@pytest.mark.integration
async def test_files_listing_has_push_row(session_factory):
    async with session_factory() as s, s.begin():
        await upsert_events(s, "demo/push", [push_row()])
    async with projects_client(session_factory, (make_project(push_source=PUSH),)) as http:
        rows = (await http.get("/api/v1/projects/demo/files")).json()
    assert [(r["name"], r["event_count"]) for r in rows] == [("push", 1)]


def test_agent_prompt_counts_push_only_project_as_not_empty():
    assert "no log files yet" not in build_system_prompt(make_project(push_source=PUSH))
    assert "no log files yet" in build_system_prompt(make_project())
