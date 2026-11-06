"""The audit golden set (tests/fixtures/agent-golden-audit.yaml) is well formed and its expected numbers are the real
ones: they are recomputed here from the fixture log with the real audit parser. It does not run the agent."""

from collections import Counter

import pytest
import yaml

from app.ingestion.fields.audit import audit_fields
from app.ingestion.projects import scan_projects
from tests.search_helpers import FIXTURES
from tests.test_agent_golden import CLASSES, FIELDS, KINDS, TOOLS

GOLDEN = FIXTURES / "agent-golden-audit.yaml"
PROJECTS = FIXTURES / "projects"
CASES = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))["cases"]
BY_ID = {case["id"]: case for case in CASES}


def audit_events() -> list[tuple[str, str, dict[str, str]]]:
    """(date, time, audit fields) of every audit event of the fixture, read through the real parser."""
    log = next((PROJECTS / "wso2-audit").glob("*.log"))
    events = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if "{AUDIT_LOG} - " not in line:
            continue
        stamp = line.split("[")[3]
        events.append((stamp[:10], stamp[11:19], audit_fields(line.split("{AUDIT_LOG} - ", 1)[1])))
    return events


def logins(result: str) -> list[tuple[str, str, dict[str, str]]]:
    return [e for e in audit_events() if e[2]["audit_action"] == "Login" and e[2]["audit_result"] == result]


def who(fields: dict[str, str]) -> str:
    return fields.get("audit_user") or fields["audit_actor"]


def test_the_fixture_project_is_valid_and_has_one_log():
    [project] = [p for p in scan_projects(PROJECTS) if p.id == "wso2-audit"]

    assert project.error is None and project.type == "wso2" and len(project.files) == 1


def test_ten_cases_with_unique_ids_and_the_smoke_set():
    assert len(CASES) == 10 and len(BY_ID) == 10
    smoke = Counter(case["class"] for case in CASES if case.get("smoke"))
    assert smoke == {"summary_only": 1, "summary_then_search": 1}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_each_case_is_well_formed(case):
    assert set(case) <= FIELDS
    assert case["class"] in CLASSES and case["kind"] in KINDS and case["question"].strip() and case["note"].strip()
    assert set(case["tools"]) <= TOOLS and set(case.get("forbid", [])) <= TOOLS
    assert all(isinstance(fact, list) and fact for fact in case["answer_has"])


def has(case_id: str, expected: str) -> bool:
    return any(expected in spellings for spellings in BY_ID[case_id]["answer_has"])


def test_the_expected_counts_are_the_ones_in_the_fixture():
    ok, failed = logins("Success"), logins("Failed")
    day = Counter(d for d, _, _ in ok)
    window = [e for e in ok if e[0] == "2026-11-04" and "10:00:00" <= e[1] <= "12:00:00"]
    actions = Counter(f["audit_action"] for _, _, f in audit_events())
    users = Counter(who(f) for _, _, f in ok)
    apps = Counter(f["audit_application"] for _, _, f in ok)

    assert (len(ok), len(failed)) == (20, 5)
    assert has("a1-logins-on-a-day", str(day["2026-11-04"]))
    assert has("a2-one-users-logins", str(users["Goutam001"]))
    top_user, top_count = users.most_common(1)[0]
    assert has("a3-most-frequent-user", top_user) and has("a3-most-frequent-user", str(top_count))
    assert users.most_common(2)[1][1] < top_count  # a unique winner
    top_app, app_count = apps.most_common(1)[0]
    assert has("a4-most-accessed-application", top_app) and has("a4-most-accessed-application", str(app_count))
    failing_app, failing_count = Counter(f["audit_application"] for _, _, f in failed).most_common(1)[0]
    assert has("a5-application-with-most-failures", failing_app) and has("a5-application-with-most-failures", str(failing_count))
    assert has("a6-daily-login-report", str(day["2026-11-03"])) and has("a6-daily-login-report", str(day["2026-11-04"]))
    named_failures = {who(f) for _, _, f in failed if "@" not in who(f)}
    assert named_failures == {"testuser1", "testuser2", "Goutam001"}
    assert all(has("a7-failed-logins-by-user", name) for name in named_failures)
    action, action_count = actions.most_common(1)[0]
    assert action == "Get-User-Claim-Values" and has("a9-most-frequent-action", str(action_count))
    assert actions.most_common(2)[1][1] < action_count
    assert has("a10-logins-in-a-time-window", str(len(window)))


def test_the_hidden_users_are_the_two_e_mail_users_and_their_addresses_are_not_expected():
    hidden = {who(f) for _, _, f in audit_events() if "@" in who(f)}

    assert hidden == {"riya.sen@example.com", "arun.das@example.com"}
    for case_id in ("a7-failed-logins-by-user", "a8-hidden-email-addresses"):
        assert "@example.com" in BY_ID[case_id]["answer_lacks"]
        assert not any("@" in spelling for spellings in BY_ID[case_id]["answer_has"] for spelling in spellings)


@pytest.mark.integration
async def test_the_indexed_fixture_gives_the_same_counts_in_sql(session_factory):
    from sqlalchemy import func, select

    from app.models.log_event import LogEvent
    from tests.search_helpers import index_file

    [project] = [p for p in scan_projects(PROJECTS) if p.id == "wso2-audit"]
    await index_file(project.files[0], session_factory)
    action, result = LogEvent.fields["audit_action"].astext, LogEvent.fields["audit_result"].astext
    app, user = LogEvent.fields["audit_application"].astext, LogEvent.fields["audit_user"].astext
    async with session_factory() as session:
        async def counts(column, *where):
            rows = await session.execute(select(column, func.count()).where(*where).group_by(column).order_by(func.count().desc()))
            return dict(rows.all())

        by_app = await counts(app, action == "Login", result == "Success")
        failed_by_app = await counts(app, action == "Login", result == "Failed")
        by_user = await counts(user, action == "Login", result == "Success")
        by_action = await counts(action, action.is_not(None))

    assert by_app == {"My Account": 10, "Console": 7, "portalApplication": 3}
    assert failed_by_app == {"portalApplication": 4, "Console": 1}
    assert by_user["Goutam001"] == 8 and by_user["admin"] == 6
    assert sum(count for name, count in by_user.items() if name.startswith("[REDACTED:email")) == 2  # the hidden user
    assert by_action["Get-User-Claim-Values"] == 31 and by_action["Login"] == 25
