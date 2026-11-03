"""Signatures are computed at index time and stored on every event (tool redesign §A)."""

import re

import pytest
from sqlalchemy import select

from app.ingestion.projects import scan_projects
from app.models.log_event import LogEvent
from tests.project_helpers import SMALL_WSO2_LOG, make_project_folder
from tests.search_helpers import index_file

pytestmark = pytest.mark.integration


async def indexed_rows(tmp_path, session_factory) -> list[LogEvent]:
    make_project_folder(tmp_path, "p", "title: P\ntype: wso2\ntimezone: Asia/Kolkata\n", SMALL_WSO2_LOG)
    [project] = scan_projects(tmp_path)
    await index_file(project.files[0], session_factory)
    async with session_factory() as session:
        return list(await session.scalars(select(LogEvent).order_by(LogEvent.locator["line_start"].as_integer())))


async def test_every_indexed_event_gets_a_signature_and_template(tmp_path, session_factory):
    rows = await indexed_rows(tmp_path, session_factory)

    assert rows and all(re.fullmatch(r"[0-9a-f]{8}", r.signature) for r in rows)
    assert all(r.signature_template and len(r.signature_template) <= 200 for r in rows)


async def test_events_of_one_kind_share_a_signature(tmp_path, session_factory):
    rows = await indexed_rows(tmp_path, session_factory)

    cache = [r for r in rows if "Cannot find AuthenticationResult from the cache" in r.raw_text]
    assert [r.locator["line_start"] for r in cache] == [67, 68]  # the golden w7 lines
    assert cache[0].signature == cache[1].signature
    assert cache[0].signature_template.startswith("ERROR {")
    assert len({r.signature for r in rows}) < len(rows)  # kinds, not one id per event
