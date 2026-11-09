"""count_by flags a group whose Login events come at a steady interval: likely automatic session renewals, not people
signing in. Found on the sagar-wso2 data, where one user had 14 Console logins, six of them 5 minutes apart."""

from datetime import UTC, datetime, timedelta

import pytest

from app.agent.tools.count_by import Counts, Group, group_counts, period_text, render_counts, steady_period
from app.ingestion.store import EventRow, upsert_events

START = datetime(2026, 11, 6, 6, 32, 47, tzinfo=UTC)
# the real offsets, in seconds, of the 14 Login events of the top user (13 gaps, six of them about 300 s)
REAL_OFFSETS = [0, 6, 305, 606, 906, 1038, 1206, 1472, 1506, 1607, 1609, 1913, 2214, 2514]


def times(*offsets: float) -> list[datetime]:
    return [START + timedelta(seconds=o) for o in offsets]


def test_evenly_spaced_events_are_steady():
    count, period = steady_period(times(*range(0, 3000, 300)))

    assert (count, round(period)) == (9, 300)


def test_the_real_mixed_pattern_is_still_found():
    count, period = steady_period(times(*REAL_OFFSETS))

    assert count == 6
    assert 295 <= period <= 305


def test_irregular_logins_are_not_steady():
    assert steady_period(times(0, 40, 400, 410, 1500, 1511, 2900, 5000)) is None


def test_too_few_events_are_not_steady():
    assert steady_period(times(0, 300, 600)) is None  # two gaps: could be chance
    assert steady_period([]) is None


def test_a_burst_of_near_simultaneous_events_is_not_a_period():
    assert steady_period(times(0, 1, 2, 3, 4, 5, 6)) is None  # gaps under the minimum period


def test_period_text():
    assert period_text(300.4) == "5 min"
    assert period_text(45) == "45 s"
    assert period_text(3600) == "60 min"


def group(key: str, n: int, steady=None) -> Group:
    return Group((key,), n, "p/push", 1, START, "p/push", 9, START + timedelta(hours=1), steady)


def rendered(rows: tuple[Group, ...]) -> str:
    counts = Counts(total=sum(g.n for g in rows), groups=len(rows), missing=0, rows=rows)
    return render_counts(counts, by=["user"], filters={"action": "Login"}, window=(START, START), project_id="p",
                         coverage="files 1, indexed 1", top=15)


def test_a_steady_group_gets_a_note_that_names_it():
    result = rendered((group("[REDACTED:email:ud611f9a77fe]", 14, steady=(6, 300.4)), group("asutosh", 1)))

    note = next(line for line in result.splitlines() if line.startswith("note:"))
    assert "[REDACTED:email:ud611f9a77fe]: 6 gaps of about 5 min between its 14 events" in note
    assert "automatic session renewals" in note
    assert "asutosh:" not in note


def test_no_steady_group_means_no_renewal_note():
    assert "renewals" not in rendered((group("asutosh", 2), group("Goutam003", 1)))


# --- against the database ---

pytestmark_db = pytest.mark.integration


def login(key: str, user: str, at: datetime) -> EventRow:
    return EventRow(event_key=key, locator={"line_start": 1}, occurred_at=at, level="INFO", raw_text=key,
                    fields={"audit_action": "Login", "audit_result": "Success", "audit_user": user,
                            "audit_application": "Console"}, semantic_text=key)


async def seed(session_factory) -> None:
    rows = [login(f"bot{i}", "bot", START + timedelta(seconds=300 * i)) for i in range(8)]
    rows += [login(f"human{i}", "human", START + timedelta(seconds=s)) for i, s in enumerate((0, 90, 2000, 2100, 7000, 7300))]
    async with session_factory() as session, session.begin():
        await upsert_events(session, "steady/push", rows)


async def counted(session_factory, by, **filters):
    async with session_factory() as session:
        return await group_counts(session, source_ids=["steady/push"], by=by, filters=filters, start=None, end=None,
                                  timezone="UTC", top=10)


@pytest.mark.integration
async def test_login_counts_by_user_flag_only_the_steady_user(session_factory):
    await seed(session_factory)

    result = await counted(session_factory, ["user"], action="Login")

    steady = {g.key[0]: g.steady for g in result.rows}
    assert steady["human"] is None
    count, period = steady["bot"]
    assert (count, round(period)) == (7, 300)


@pytest.mark.integration
async def test_nothing_is_flagged_without_the_login_filter_or_for_other_groupings(session_factory):
    await seed(session_factory)

    unfiltered = await counted(session_factory, ["user"])
    by_day = await counted(session_factory, ["day"], action="Login")

    assert all(g.steady is None for g in unfiltered.rows + by_day.rows)
