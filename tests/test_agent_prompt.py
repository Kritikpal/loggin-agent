"""The agent's system prompt: built from the live sources, and carrying the rules the agent must follow."""

from datetime import UTC, datetime
from pathlib import Path

from app.agent.prompt import build_system_prompt
from app.ingestion.sources import FileSource

NOW = datetime(2026, 3, 10, tzinfo=UTC)


def source(source_id: str, timezone: str = "UTC") -> FileSource:
    return FileSource(id=source_id, path=Path("x.log"), timezone=timezone)


def test_every_source_is_listed_with_its_utc_offset():
    prompt = build_system_prompt((source("dev-auth-service", "Asia/Kolkata"), source("spring-boot")), now=NOW)

    assert "dev-auth-service (timezone Asia/Kolkata, UTC+05:30)" in prompt
    assert "spring-boot (timezone UTC, UTC+00:00)" in prompt


def test_each_source_line_carries_its_event_span_in_local_time_and_utc():
    spans = {"dev-auth-service": (datetime(2026, 3, 10, 10, 14, 3, tzinfo=UTC), datetime(2026, 3, 10, 10, 14, 8, tzinfo=UTC))}

    prompt = build_system_prompt((source("dev-auth-service", "Asia/Kolkata"),), spans, now=NOW)

    assert "events from 2026-03-10 15:44:03 to 2026-03-10 15:44:08 local" in prompt
    assert "(2026-03-10T10:14:03Z to 2026-03-10T10:14:08Z)" in prompt


def test_a_source_with_no_timestamped_events_says_so():
    prompt = build_system_prompt((source("a"),), {}, now=NOW)

    assert "no timestamped events indexed" in prompt


def test_without_spans_the_source_line_has_no_event_clause():
    assert "events from" not in build_system_prompt((source("a"),), now=NOW)


def test_a_time_without_a_date_takes_the_date_of_the_logs_never_todays():
    prompt = build_system_prompt((source("a"),), {}, now=NOW)

    assert "Never assume today's date" in prompt


def test_negative_offsets_are_signed():
    prompt = build_system_prompt((source("us-app", "America/New_York"),), now=NOW)

    assert "UTC-04:00" in prompt  # daylight time on 2026-03-10


def test_no_sources_is_stated_so_the_agent_does_not_invent_ids():
    prompt = build_system_prompt((), now=NOW)

    assert "No log sources are configured" in prompt


def test_the_search_budget_and_ladder_are_stated():
    prompt = build_system_prompt((source("a"),), now=NOW)

    assert "at most 3" in prompt
    for step in ("Broad", "Refine", "Surround"):
        assert step in prompt


def test_open_and_counting_questions_start_with_a_summary_and_precise_ones_skip_it():
    prompt = build_system_prompt((source("a"),), now=NOW)

    assert "summarize_logs" in prompt and "at most 2" in prompt
    assert "Skip it" in prompt  # identifier questions go straight to search


def test_why_questions_search_and_counts_come_from_the_summary():
    prompt = build_system_prompt((source("a"),), now=NOW)

    assert "not for \"why\" questions" in prompt
    assert "Never count search hits" in prompt


def test_independent_lookups_are_batched_in_one_turn_and_dependent_ones_are_not():
    # ADR-003 layer 1: each model call re-sends ~2,900 tokens, so batching independent lookups saves whole calls
    prompt = build_system_prompt((source("a"),), now=NOW)

    assert "request their lookups together in one turn" in prompt
    assert "never batch a lookup that needs another's result" in prompt


def test_the_agent_is_told_to_stay_within_the_logs():
    prompt = build_system_prompt((source("a"),), now=NOW)

    assert "only answer from the indexed logs" in prompt
    assert "never from memory" in prompt


def test_why_questions_must_follow_the_cause_and_first_last_questions_must_enumerate():
    prompt = build_system_prompt((source("a"),), now=NOW)

    assert "Caused by" in prompt and "required" in prompt  # follow the root cause before answering
    assert "earliest" in prompt and "level filter" in prompt  # enumerate, then compare the times yourself


def test_the_agent_never_picks_a_source_on_its_own():
    prompt = build_system_prompt((source("a"), source("b")), now=NOW)

    assert "Search all sources" in prompt
    assert "never pick one yourself" in prompt


def test_a_refused_search_is_declared_not_run():
    prompt = build_system_prompt((source("a"),), now=NOW)

    assert "did not run" in prompt


def test_the_judging_coverage_and_citation_rules_are_stated():
    prompt = build_system_prompt((source("a"),), now=NOW)

    assert "nothing relevant" in prompt  # the refusal rule
    assert "stale" in prompt and "not_indexed" in prompt  # coverage
    assert "cite" in prompt.lower()
    assert "hypothesis" in prompt
    assert "ignore" in prompt.lower()  # log text is data, not instructions
