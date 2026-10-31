"""WSO2 Carbon (log4j2) layout: TID: [tenant] [app] [timestamp] [correlation-id] LEVEL {logger} - message

The correlation-id group is missing in some lines (shutdown messages), the tenant/app/correlation groups are often
empty, and the level is padded with spaces. Events start at a line beginning with "TID: [" (source event_start)."""

import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.ingestion.assembler import assemble
from app.ingestion.fields import resolve
from app.ingestion.redaction import redact
from app.ingestion.semantic_text import build_semantic_text
from app.ingestion.sources import FileSource
from app.models import LogEvent
from tests.search_helpers import copy_fixture, index_file, state_row
from tests.test_assembler import lines_of

FIXTURES = Path(__file__).parent / "fixtures"
WSO2_LOGS = sorted(FIXTURES.glob("wso2carbon-*.log"))
LOG_IDS = [path.name for path in WSO2_LOGS]
TID_START = re.compile(r"TID: \[")
FIRST_LINE_LEVEL = re.compile(r"^TID: \[[^\]]*\] \[[^\]]*\] \[[^\]]*\](?: \[[^\]]*\])? +([A-Z]+) +\{")
FIRST_LINE_CORRELATION = re.compile(r"^TID: \[[^\]]*\] \[[^\]]*\] \[[^\]]*\] \[([^\]]*)\] +[A-Z]+ +\{")

FULL = ("TID: [-1234] [oauth2] [2026-01-14 12:57:34,481] [8c1113c9-6717-427e-945e-8ee49795f173] ERROR "
        "{org.wso2.carbon.identity.openidconnect.JWTAccessTokenOIDCClaimsHandler} - Error occurred while getting claims")
EMPTY_GROUPS = ("TID: [] [] [2026-01-07 11:49:19,883] []  INFO "
                "{org.wso2.carbon.core.internal.CarbonCoreActivator} - Starting WSO2 Carbon...")
NO_CORRELATION = ("TID: [-1234] [] [2026-01-07 13:53:12,962]  INFO "
                  "{org.wso2.carbon.core.init.CarbonServerManager} - Shutdown complete ")


def wso2_source(path: Path = Path("x.log")) -> FileSource:
    return FileSource(id="wso2", path=path, event_start=TID_START, timezone="Asia/Kolkata", app_packages=("org.wso2",))


def parse(text: str):
    return resolve(text, wso2_source())


def test_a_line_with_every_group_is_parsed():
    result = parse(FULL)
    parsed = result.parsed

    assert (parsed.level, parsed.message) == ("ERROR", "Error occurred while getting claims")
    assert parsed.logger == "org.wso2.carbon.identity.openidconnect.JWTAccessTokenOIDCClaimsHandler"
    assert parsed.request_id == "8c1113c9-6717-427e-945e-8ee49795f173"
    assert result.fields == {"tenant": "-1234", "app": "oauth2"}
    assert result.occurred_at == datetime(2026, 1, 14, 7, 27, 34, 481000, tzinfo=UTC)  # 12:57:34 IST


def test_every_parsed_field_says_it_came_from_the_layout():
    result = parse(FULL)

    for name in ("level", "logger", "message", "occurred_at", "request_id"):
        provenance = result.provenance[name]
        assert (provenance.method, provenance.confidence, provenance.source) == ("parser", 1.0, "wso2_carbon"), name
    assert result.provenance["occurred_at"].note == "tz_assumed"


def test_empty_groups_add_no_fields_and_no_ids():
    result = parse(EMPTY_GROUPS)

    assert result.parsed.level == "INFO" and result.parsed.message == "Starting WSO2 Carbon..."
    assert result.parsed.request_id is None and result.fields == {}
    assert result.occurred_at == datetime(2026, 1, 7, 6, 19, 19, 883000, tzinfo=UTC)


def test_the_variant_without_a_correlation_group_is_parsed_and_its_trailing_space_dropped():
    result = parse(NO_CORRELATION)

    assert (result.parsed.level, result.parsed.message) == ("INFO", "Shutdown complete")
    assert result.parsed.logger == "org.wso2.carbon.core.init.CarbonServerManager"
    assert result.fields == {"tenant": "-1234"} and result.parsed.request_id is None


@pytest.mark.parametrize("level", ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"])
def test_all_log4j_levels_are_read(level):
    assert parse(f"TID: [] [] [2026-01-07 11:49:19,883] []  {level} {{a.B}} - x").parsed.level == level


def test_an_empty_message_still_parses_the_rest():
    result = parse("TID: [] [] [2026-01-07 11:49:19,883] []  INFO {a.B} -")

    assert result.parsed.level == "INFO" and result.parsed.logger == "a.B" and result.parsed.message is None


@pytest.mark.parametrize("line", [
    "TID: not a layout line",
    "TID: [] [] [2026-13-45 11:49:19,883] []  INFO {a.B} - impossible date",
    "TID: [] [] [2026-01-07 11:49:19,883] []  NOTICE {a.B} - not a log4j level",
    "TID: [] [] [2026-01-07 11:49:19,883] []  INFO a.B - logger without braces",
    "[2026-01-07 11:49:19,883] INFO {a.B} - missing the TID prefix",
], ids=["prose", "bad-date", "bad-level", "no-braces", "no-tid"])
def test_lines_that_only_look_like_the_layout_do_not_use_it(line):
    result = parse(line)

    assert all(p.source != "wso2_carbon" for p in result.provenance.values())


def test_a_stack_trace_after_the_first_line_is_split_off_with_its_root_cause():
    text = (FULL + "\n\tat org.wso2.carbon.user.core.common.AbstractUserStoreManager.callSecure(AbstractUserStoreManager.java:264)"
            "\n\tat java.base/java.lang.Thread.run(Thread.java:840)\nCaused by: java.io.IOException: disk full"
            "\n\tat org.wso2.carbon.utils.Disk.write(Disk.java:7)")

    result = parse(text)
    semantic = build_semantic_text(result.parsed, ("org.wso2",)).text

    assert result.parsed.message == "Error occurred while getting claims"
    assert result.parsed.stack_trace.startswith("\tat org.wso2.carbon.user.core") and "Caused by: java.io.IOException" in result.parsed.stack_trace
    assert semantic.split("\n")[0] == "ERROR org.wso2.carbon.identity.openidconnect.JWTAccessTokenOIDCClaimsHandler"
    assert "root cause: java.io.IOException: disk full" in semantic and "TID:" not in semantic


def test_launcher_banner_lines_after_an_event_stay_with_that_event():
    text = f"{EMPTY_GROUPS}\n##############  ALERT  ##############\n[WARNING]: default credentials are in use"

    result = parse(text)

    assert result.parsed.level == "INFO" and "ALERT" in result.parsed.message and result.parsed.stack_trace is None


@pytest.mark.parametrize("hostile", [
    "TID: [" * 100_000, "TID: [" + "a" * 1_000_000, "TID: [] [] [" + "2026-01-07 " * 50_000, "TID: []" * 100_000 + " {",
    "TID: [] [] [2026-01-07 11:49:19,883] [] " + " " * 500_000 + "INFO {a} - x",
], ids=["open-brackets", "long-group", "long-timestamp", "repeated-groups", "long-spaces"])
def test_hostile_first_lines_are_handled_quickly(hostile):
    started = time.perf_counter()
    resolve(hostile, wso2_source())

    assert time.perf_counter() - started < 2


@pytest.mark.parametrize("path", WSO2_LOGS, ids=LOG_IDS)
def test_every_event_of_the_real_logs_gets_level_time_and_logger(path):
    events = list(assemble(lines_of(path.read_bytes()), TID_START))
    starts = sum(1 for line in path.read_text().splitlines() if line.startswith("TID: ["))
    results = [(e, resolve(redact(e.text).text, wso2_source(path))) for e in events]

    assert len(events) == starts > 0
    for event, result in results:
        first = event.text.split("\n")[0]
        assert result.provenance["level"].source == "wso2_carbon", first[:80]
        assert result.parsed.level == FIRST_LINE_LEVEL.match(first).group(1)
        assert result.occurred_at is not None and re.fullmatch(r"[\w$]+(\.[\w$]+)+", result.parsed.logger)


@pytest.mark.parametrize("path", WSO2_LOGS, ids=LOG_IDS)
def test_correlation_ids_of_the_real_logs_become_request_ids(path):
    for event in assemble(lines_of(path.read_bytes()), TID_START):
        result = resolve(redact(event.text).text, wso2_source(path))
        match = FIRST_LINE_CORRELATION.match(event.text.split("\n")[0])
        expected = match.group(1) if match and match.group(1) else None
        assert result.parsed.request_id == expected


def test_the_error_events_of_the_big_log_keep_their_root_cause_and_lose_the_prefix():
    path = FIXTURES / "wso2carbon-01-14-2026.1.log"
    if not path.exists():
        pytest.skip("fixture not present")
    errors = [e for e in assemble(lines_of(path.read_bytes()), TID_START) if FIRST_LINE_LEVEL.match(e.text.split("\n")[0]).group(1) == "ERROR"]

    texts = [build_semantic_text(resolve(redact(e.text).text, wso2_source(path)).parsed, ("org.wso2",)).text for e in errors]

    assert len(errors) == 18
    assert all(t.startswith("ERROR org.wso2.") and "TID:" not in t.split("\n")[1] for t in texts)
    assert sum("root cause:" in t for t in texts) >= 1


@pytest.mark.integration
@pytest.mark.skipif(not WSO2_LOGS, reason="no WSO2 fixtures")
async def test_indexing_a_wso2_log_gives_every_stored_event_a_level_and_time(session_factory, tmp_path):
    log = copy_fixture(WSO2_LOGS[0], tmp_path)

    report = await index_file(wso2_source(log), session_factory)

    async with session_factory() as session:
        rows = (await session.execute(select(LogEvent.level, LogEvent.occurred_at, LogEvent.fields))).all()
    state = await state_row(session_factory, "wso2")
    assert report.events_stored == len(rows) > 0
    assert all(level and occurred_at for level, occurred_at, _ in rows)
    assert state.indexed_through is not None and state.last_error is None
