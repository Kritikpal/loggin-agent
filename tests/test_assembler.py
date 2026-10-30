import re

from app.ingestion.assembler import (
    DEFAULT_EVENT_START, EVERY_LINE, AssemblyLimits, assemble, start_pattern_for,
)
from app.ingestion.sanitize import sanitize
from tests.test_semantic_text import FIXTURES


def lines_of(data: bytes) -> list[tuple[int, bytes]]:
    """What the file reader will hand over: (byte offset, line including its terminator)."""
    out, offset = [], 0
    for raw in data.splitlines(keepends=True):
        out.append((offset, raw))
        offset += len(raw)
    return out


def events_of(text: str, pattern=DEFAULT_EVENT_START, **kwargs):
    return list(assemble(lines_of(text.encode()), pattern, **kwargs))


def test_the_spring_fixture_gives_one_event_per_timestamp_line_with_the_trace_in_one_of_them():
    data = (FIXTURES / "spring_boot.log").read_bytes()

    events = list(assemble(lines_of(data), DEFAULT_EVENT_START))

    timestamp_lines = sum(bool(re.match(rb"\d{4}-\d\d-\d\dT", line)) for line in data.splitlines())
    assert len(events) == timestamp_lines == 15
    npe = next(e for e in events if "NullPointerException: Cannot invoke" in e.text and "\tat " in e.text)
    assert npe.text.startswith("2026-10-29T08:16:05.893Z ERROR") and "\n\njava.lang.NullPointerException" in npe.text
    assert npe.text.count("\tat ") == 11 and npe.end_line - npe.start_line + 1 == 14
    before = events[events.index(npe) - 1]
    assert before.text.startswith("2026-10-29T08:16:05.892Z") and "\n" not in before.text


def test_offsets_chain_exactly_and_point_at_the_original_bytes():
    data = (FIXTURES / "spring_boot.log").read_bytes()

    events = list(assemble(lines_of(data), DEFAULT_EVENT_START))

    assert events[0].start_offset == 0 and events[-1].end_offset == len(data)
    assert all(a.end_offset == b.start_offset for a, b in zip(events, events[1:]))
    for event in events:  # the bytes at the offsets are exactly the event text plus its trailing line breaks
        assert data[event.start_offset:event.end_offset].decode().rstrip("\r\n") == event.text


def test_a_trace_with_caused_by_suppressed_and_blank_lines_is_one_event():
    log = (
        "2026-10-29T10:00:00Z ERROR boom\n\njava.lang.IllegalStateException: x\n\tat a.B.c(B.java:1)\n"
        "Caused by: java.io.IOException: y\n\tat d.E.f(E.java:2)\n\t... 42 more\n\tSuppressed: java.lang.Error: z\n"
        "\t\tat g.H.i(H.java:3)\n\n2026-10-29T10:00:01Z INFO next\n"
    )

    first, second = events_of(log)

    assert "Suppressed" in first.text and "Caused by" in first.text and first.text.count("\n\n") == 1  # the trailing blank line is trimmed but stays inside the offsets
    assert first.end_offset == second.start_offset
    assert second.text == "2026-10-29T10:00:01Z INFO next"


def test_lines_before_the_first_start_line_become_their_own_event():
    orphan, event = events_of("leftover tail of a trace\n\tat x.Y.z(Y.java:1)\n2026-10-29T10:00:00Z INFO hi\n")

    assert orphan.text == "leftover tail of a trace\n\tat x.Y.z(Y.java:1)" and orphan.start_line == 1
    assert event.text.startswith("2026-10-29T10:00:00Z") and event.start_line == 3


def test_crlf_line_endings_keep_offsets_exact_and_leave_no_carriage_returns():
    data = b"2026-10-29T10:00:00Z INFO a\r\n  more\r\n2026-10-29T10:00:01Z INFO b\r\n"

    first, second = assemble(lines_of(data), DEFAULT_EVENT_START)

    assert first.text == "2026-10-29T10:00:00Z INFO a\n  more" and "\r" not in first.text
    assert (first.end_offset, second.start_offset, second.end_offset) == (len(b"2026-10-29T10:00:00Z INFO a\r\n  more\r\n"),) * 2 + (len(data),)


def test_a_huge_event_is_split_at_the_line_cap_and_flagged():
    log = "2026-10-29T10:00:00Z ERROR big\n" + "\tat a.B.c(B.java:1)\n" * 10_000

    events = events_of(log)

    assert len(events) == 10_000 // 499 + 1 and events[0].truncated
    assert all(len(e.text.split("\n")) <= 500 for e in events) and events[-1].end_offset == len(log.encode())
    assert all(a.end_offset == b.start_offset for a, b in zip(events, events[1:]))


def test_a_giant_single_line_is_clipped_to_the_character_cap():
    (event,) = events_of("2026-10-29T10:00:00Z ERROR " + "x" * 1_000_000 + "\n", limits=AssemblyLimits(max_chars=1000))

    assert len(event.text) == 1000 and event.truncated and event.end_offset > 1_000_000


def test_an_event_just_inside_the_caps_is_not_flagged():
    (event,) = events_of("2026-10-29T10:00:00Z ERROR ok\n\tat a.B.c(B.java:1)\n")

    assert not event.truncated


def test_control_characters_and_ansi_codes_are_removed_from_events():
    (event,) = events_of("2026-10-29T10:00:00Z \x1b[31mERROR\x1b[0m bad\x00byte\n")

    assert event.text == "2026-10-29T10:00:00Z ERROR badbyte"


def test_invalid_utf8_is_replaced_not_fatal():
    (event,) = assemble([(0, b"2026-10-29T10:00:00Z ERROR caf\xe9\n")], DEFAULT_EVENT_START)

    assert "caf�" in event.text


def test_empty_input_gives_no_events():
    assert list(assemble([], DEFAULT_EVENT_START)) == []


def test_line_numbers_start_where_the_caller_says():
    first, second = events_of("2026-10-29T10:00:00Z a\n  x\n2026-10-29T10:00:01Z b\n", first_line=101)

    assert (first.start_line, first.end_line, second.start_line, second.end_line) == (101, 102, 103, 103)


def test_the_default_start_pattern_accepts_both_iso_forms_and_ignores_other_lines():
    assert DEFAULT_EVENT_START.match("2026-10-29T08:15:23.412Z INFO x")
    assert DEFAULT_EVENT_START.match("2026-10-29 08:15:23,412 INFO x")
    assert not DEFAULT_EVENT_START.match("\tat com.acme.A.f(A.java:1)")
    assert not DEFAULT_EVENT_START.match("Caused by: x")


def test_json_sources_make_every_non_blank_line_an_event_and_a_custom_pattern_wins():
    assert start_pattern_for("json", None) is EVERY_LINE
    assert start_pattern_for("text", None) is DEFAULT_EVENT_START
    custom = re.compile(r"\[\d+\]")
    assert start_pattern_for("text", custom) is custom
    assert [e.text for e in events_of('{"a":1}\n{"a":2}\n', EVERY_LINE)] == ['{"a":1}', '{"a":2}']
    assert [e.text for e in events_of("[1] a\n  b\n[2] c\n", custom)] == ["[1] a\n  b", "[2] c"]


def test_a_hostile_start_pattern_only_ever_sees_the_start_of_a_line():
    seen = []

    class Spy:
        def match(self, text):
            seen.append(len(text))

    list(assemble([(0, b"x" * 100_000 + b"\n")], Spy()))  # type: ignore[arg-type]

    assert seen == [500]


def test_sanitize_keeps_tabs_newlines_and_unicode_and_is_idempotent():
    text = "a\tb\nc é 日本 \x1b[1;31mred\x1b[0m\x00\x07\x1b]0;title\x07end\x7f"

    once = sanitize(text)

    assert once == "a\tb\nc é 日本 redend" and sanitize(once) == once
