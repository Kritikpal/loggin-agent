"""Regression tests for the security review of assemble(): edge cases around caps, blank lines and indentation."""

from app.ingestion.assembler import DEFAULT_EVENT_START, EVERY_LINE, AssemblyLimits, assemble
from tests.test_assembler import events_of, lines_of


def test_an_indented_json_line_still_starts_an_event():
    events = events_of('{"a":1}\n  {"b":2}\n\t{"c":3}\n', EVERY_LINE)

    assert [e.text for e in events] == ['{"a":1}', '  {"b":2}', '\t{"c":3}']


def test_the_character_cap_counts_the_newlines_between_lines():
    log = "2026-10-29T10:00:00Z a\n" + "123456789\n" * 40

    events = events_of(log, limits=AssemblyLimits(max_chars=100))

    assert all(len(e.text) <= 100 for e in events) and len(events) > 1
    assert all(a.end_offset == b.start_offset for a, b in zip(events, events[1:]))


def test_leading_blank_lines_join_the_first_event_and_are_covered_by_its_offsets():
    data = b"\n\n2026-10-29T10:00:00Z INFO a\n2026-10-29T10:00:01Z INFO b\n"

    first, second = assemble(lines_of(data), DEFAULT_EVENT_START)

    assert first.text == "2026-10-29T10:00:00Z INFO a" and first.start_offset == 0 and first.start_line == 1
    assert first.end_offset == second.start_offset


def test_events_with_no_text_are_not_emitted():
    assert events_of("\n" * 1000) == []
    assert events_of("\n\n   \n") == []
    assert [e.text for e in events_of("2026-10-29T10:00:00Z a\n\n\n\n")] == ["2026-10-29T10:00:00Z a"]


def test_a_line_longer_than_the_cap_is_clipped_but_its_offsets_still_cover_it():
    data = b"abcdefghij\n"

    (event,) = assemble([(0, data)], DEFAULT_EVENT_START, AssemblyLimits(max_chars=5))

    assert event.text == "abcde" and event.truncated and (event.start_offset, event.end_offset) == (0, len(data))


def test_a_cap_split_keeps_offsets_chained_and_flags_the_cut_event():
    log = "2026-10-29T10:00:00Z ERROR x\n" + "\tat a.B.c(B.java:1)\n" * 20

    first, *rest = events_of(log, limits=AssemblyLimits(max_lines=5))

    assert first.truncated and first.end_line == 5 and rest[0].start_line == 6
    assert first.end_offset == rest[0].start_offset
