"""count_by: exact counts by field over the audit fixture (tests/fixtures/projects/wso2-audit, known answers)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.agent.tools.count_by import group_counts
from app.ingestion.projects import scan_projects
from app.ingestion.store import EventRow, upsert_events
from tests.search_helpers import FIXTURES, index_file

pytestmark = pytest.mark.integration

PROJECTS = FIXTURES / "projects"


async def indexed(session_factory):
    [project] = [p for p in scan_projects(PROJECTS) if p.id == "wso2-audit"]
    await index_file(project.files[0], session_factory)
    return project


async def counts(session_factory, project, by, *, top=50, start=None, end=None, source_ids=None, **filters):
    async with session_factory() as session:
        return await group_counts(
            session, source_ids=source_ids or [s.id for s in project.sources], by=by, filters=filters, start=start,
            end=end, timezone=project.timezone, top=top)


def table(result) -> dict[tuple[str, ...], int]:
    return {group.key: group.n for group in result.rows}


async def test_logins_by_application(session_factory):
    project = await indexed(session_factory)

    result = await counts(session_factory, project, ["application"], action="Login", result="Success")

    assert [(g.key, g.n) for g in result.rows] == [(("My Account",), 10), (("Console",), 7), (("portalApplication",), 3)]
    assert (result.total, result.groups) == (20, 3)


async def test_failed_logins_by_application(session_factory):
    project = await indexed(session_factory)

    result = await counts(session_factory, project, ["application"], action="Login", result="Failed")

    assert table(result) == {("portalApplication",): 4, ("Console",): 1}


async def test_users_by_successful_logins(session_factory):
    project = await indexed(session_factory)

    result = await counts(session_factory, project, ["user"], action="Login", result="Success")

    assert [(g.key, g.n) for g in result.rows[:2]] == [(("Goutam001",), 8), (("admin",), 6)]
    assert result.groups == 5
    hidden = [g for g in result.rows if g.key[0].startswith("[REDACTED:email")]
    assert [g.n for g in hidden] == [2]  # the user who signs in with an address, with or without a pseudonym code


async def test_day_and_result_cross_table(session_factory):
    project = await indexed(session_factory)

    result = await counts(session_factory, project, ["day", "result"], action="Login")

    assert table(result) == {
        ("2026-11-03", "Success"): 6, ("2026-11-03", "Failed"): 2, ("2026-11-04", "Success"): 9,
        ("2026-11-04", "Failed"): 1, ("2026-11-05", "Success"): 5, ("2026-11-05", "Failed"): 2}


async def test_the_most_frequent_action(session_factory):
    project = await indexed(session_factory)

    result = await counts(session_factory, project, ["action"])

    assert [(g.key, g.n) for g in result.rows[:2]] == [(("Get-User-Claim-Values",), 31), (("Login",), 25)]


async def test_time_window_in_project_timezone(session_factory):
    project = await indexed(session_factory)
    start, end = datetime(2026, 11, 4, 4, 30, tzinfo=UTC), datetime(2026, 11, 4, 6, 30, tzinfo=UTC)  # 10:00-12:00 IST

    result = await counts(session_factory, project, ["result"], start=start, end=end, action="Login", result="Success")

    assert result.total == 5


async def test_filters_are_case_insensitive(session_factory):
    project = await indexed(session_factory)

    result = await counts(session_factory, project, ["application"], action="login", result="SUCCESS")

    assert result.total == 20


async def test_events_without_the_field_are_missing_not_a_group(session_factory):
    project = await indexed(session_factory)

    result = await counts(session_factory, project, ["application"])

    assert result.missing > 0 and all(g.key[0] for g in result.rows)
    assert result.total + result.missing == 80  # every event of the fixture, audit or not
    assert result.total == 24 + 21 + 11  # events naming an application: Console, My Account, portalApplication


async def test_top_cuts_the_rows_but_not_the_group_count(session_factory):
    project = await indexed(session_factory)

    result = await counts(session_factory, project, ["application"], action="Login", result="Success", top=2)

    assert len(result.rows) == 2 and result.groups == 3


async def test_hostile_filter_values_are_only_values(session_factory):
    project = await indexed(session_factory)

    hostile = await counts(session_factory, project, ["user"], user="x'; drop table log_events;--", application="%")
    after = await counts(session_factory, project, ["action"])

    assert hostile.total == 0 and after.total > 0
    async with session_factory() as session:
        assert await session.scalar(text("select count(*) from log_events")) == 80


async def test_day_boundary_uses_the_project_timezone(session_factory):
    project = await indexed(session_factory)
    fields = {"audit_action": "Login", "audit_result": "Success"}

    def event(key: str, at: datetime) -> EventRow:
        return EventRow(event_key=key, locator={"line_start": 1}, occurred_at=at, level="INFO", raw_text=key,
                        fields=fields, semantic_text=key)

    async with session_factory() as session, session.begin():  # 23:30 IST on 4 Nov and 00:10 IST on 5 Nov
        await upsert_events(session, "edge/push", [event("a", datetime(2026, 11, 4, 18, 0, tzinfo=UTC)),
                                                   event("b", datetime(2026, 11, 4, 18, 40, tzinfo=UTC))])

    result = await counts(session_factory, project, ["day"], source_ids=["edge/push"])

    assert table(result) == {("2026-11-04",): 1, ("2026-11-05",): 1}


async def test_other_sources_are_not_counted(session_factory):
    project = await indexed(session_factory)
    async with session_factory() as session, session.begin():
        await upsert_events(session, "other/push", [EventRow(
            event_key="x", locator={"line_start": 1}, occurred_at=datetime(2026, 11, 4, tzinfo=UTC), level="INFO",
            raw_text="x", fields={"audit_action": "Login", "audit_result": "Success", "audit_application": "Console"},
            semantic_text="x")])

    result = await counts(session_factory, project, ["application"], action="Login", result="Success")

    assert result.total == 20


async def test_level_and_hour_dimensions_work_without_audit_fields(session_factory):
    project = await indexed(session_factory)

    levels = await counts(session_factory, project, ["level"])
    hours = await counts(session_factory, project, ["hour"])

    assert {"INFO", "WARN"} <= {g.key[0] for g in levels.rows}
    assert all(len(g.key[0]) == 16 and g.key[0].endswith(":00") for g in hours.rows)  # "2026-11-04 10:00"
