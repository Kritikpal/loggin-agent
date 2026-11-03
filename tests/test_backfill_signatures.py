"""Backfill: events indexed before signatures existed (or under older rules) get the current signature in place."""

import pytest
from sqlalchemy import select, update

from app.ingestion.projects import scan_projects
from app.models.log_event import LogEvent
from scripts.backfill_signatures import backfill
from tests.project_helpers import SMALL_WSO2_LOG, make_project_folder
from tests.search_helpers import index_file

pytestmark = pytest.mark.integration


async def signatures(session_factory) -> dict[int, tuple[str | None, str | None]]:
    async with session_factory() as session:
        rows = await session.execute(select(LogEvent.locator, LogEvent.signature, LogEvent.signature_template))
        return {loc["line_start"]: (sig, tpl) for loc, sig, tpl in rows}


async def test_missing_and_stale_signatures_are_recomputed_without_touching_anything_else(tmp_path, session_factory):
    make_project_folder(tmp_path, "p", "title: P\ntype: wso2\ntimezone: Asia/Kolkata\n", SMALL_WSO2_LOG)
    [project] = scan_projects(tmp_path)
    await index_file(project.files[0], session_factory)
    expected = await signatures(session_factory)
    async with session_factory() as session, session.begin():
        count_before = len(list(await session.scalars(select(LogEvent.id))))
        await session.execute(update(LogEvent).values(signature=None, signature_template=None))  # pre-0003 rows
        await session.execute(update(LogEvent).where(LogEvent.locator["line_start"].as_integer() == 67)
                              .values(signature="0ld0ld00", signature_template="old rules"))  # older rules

    changed = await backfill(session_factory, project.files, batch_size=10)

    assert changed == count_before
    assert await signatures(session_factory) == expected
    async with session_factory() as session:
        assert len(list(await session.scalars(select(LogEvent.id)))) == count_before  # nothing added or removed


async def test_a_second_run_changes_nothing(tmp_path, session_factory):
    make_project_folder(tmp_path, "p", "title: P\ntype: wso2\n", SMALL_WSO2_LOG)
    [project] = scan_projects(tmp_path)
    await index_file(project.files[0], session_factory)

    assert await backfill(session_factory, project.files) == 0
