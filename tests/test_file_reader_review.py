"""Regression tests for the independent review of FileReader."""

import hashlib

import pytest

from app.ingestion.readers.base import Cursor
from tests.test_file_reader import FRAMES, HOLD, read_all, reader, ts


def test_a_buffer_full_of_blank_lines_is_stepped_over_instead_of_looping_forever(tmp_path):
    log = tmp_path / "blank.log"
    log.write_text("\n" * 40 + ts(1))

    events, cursor, _ = read_all(reader(log, HOLD, max_read_bytes=8))  # read_all fails if has_more never clears

    assert cursor.offset > 0 and cursor.line > 1


@pytest.mark.parametrize("max_events", [0, -1, -100])
def test_a_non_positive_max_events_still_makes_progress(tmp_path, max_events):
    log = tmp_path / "a.log"
    log.write_text("".join(ts(n) for n in range(5)))

    events, *_ = read_all(reader(log), max_events=max_events)

    assert len(events) == 5


@pytest.mark.parametrize("damaged", [
    '{"offset":10,"line":1,"head_len":5,"head_sha":"","file_id":""}',  # head_len must be min(256, offset)
    '{"offset":10,"line":1,"head_len":1000000000000,"head_sha":"","file_id":""}',
    '{"offset":1000,"line":1,"head_len":1000000000,"head_sha":"","file_id":""}',
    '{"offset":1000,"line":1,"head_len":10,"head_sha":"","file_id":""}',
    '{"offset":1000,"line":0,"head_len":256,"head_sha":"","file_id":""}',
    '{"offset":1000,"line":1,"head_len":256,"head_sha":5,"file_id":""}',
    '{"offset":true,"line":1,"head_len":1,"head_sha":"","file_id":""}',
    '{"offset":1000,"line":1,"head_len":256,"head_sha":"","file_id":null}',
    '{"offset":1e3,"line":1,"head_len":256,"head_sha":"","file_id":""}',
])
def test_a_cursor_from_the_database_that_makes_no_sense_is_discarded(damaged):
    assert Cursor.from_json(damaged) == Cursor()


def test_a_consistent_cursor_is_accepted():
    cursor = Cursor(offset=5000, line=40, head_len=256, head_sha="ab", file_id="1:2")

    assert Cursor.from_json(cursor.to_json()) == cursor


def test_line_numbers_stay_true_when_an_unterminated_line_is_completed_later(tmp_path):
    log = tmp_path / "a.log"
    log.write_text(ts(1, "INFO a") + ts(2, "INFO par").rstrip("\n"))
    first = reader(log, 0).read_batch()
    with log.open("a") as f:
        f.write("tial\n" + ts(3, "INFO z"))

    second = reader(log, 0).read_batch(first.next_cursor)

    assert [(e.event.start_line, e.event.text.split()[-1]) for e in first.events] == [(1, "a"), (2, "par")]
    assert [(e.event.start_line, e.event.text.split()[-1]) for e in second.events] == [(2, "tial"), (3, "z")]


def test_the_stored_fingerprint_always_matches_the_file_whatever_the_offset(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("")
    cursor = Cursor()
    for step in range(40):
        with log.open("a") as f:
            f.write(ts(step, "INFO " + "x" * (step % 17)))
        result = reader(log, 0).read_batch(cursor)
        cursor = result.next_cursor
        head = log.read_bytes()[:cursor.head_len]
        assert result.status == "ok" and cursor.head_len == min(256, cursor.offset)
        assert cursor.head_sha == (hashlib.sha256(head).hexdigest() if head else "")
    assert cursor.offset > 256  # the loop crossed the fingerprint size


def test_a_blank_prefix_and_held_events_resume_to_the_same_events_as_one_pass(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("\n\n" + "".join(ts(n, "ERROR x") + FRAMES * (n % 4) for n in range(40)))

    single, *_ = read_all(reader(log))
    held, *_ = read_all(reader(log, HOLD), max_events=3)
    flushed, *_ = read_all(reader(log), read_all(reader(log, HOLD), max_events=3)[1])

    assert [e.event_key for e in single] == [e.event_key for e in held + flushed]
