import hashlib
import os
import random
import shutil
import threading
import time

import pytest

from app.ingestion.readers.base import Cursor
from app.ingestion.readers.file_reader import FileReader
from app.ingestion.sources import FileSource
from tests.test_semantic_text import FIXTURES

HOLD = 1e12  # settle time so long the newest event is always held back
FRAMES = "\tat com.acme.A.f(A.java:1)\n"


def ts(n: int, text: str = "INFO msg") -> str:
    return f"2026-10-29T10:{n // 60:02d}:{n % 60:02d}Z {text}\n"


def reader(path, settle: float = 0.0, fmt: str = "text", **kwargs) -> FileReader:
    return FileReader(FileSource(id="t", path=path, format=fmt), settle_seconds=settle, **kwargs)


def read_all(r: FileReader, cursor=None, max_events: int = 1000):
    events, statuses = [], []
    for _ in range(100_000):
        result = r.read_batch(cursor, max_events)
        events += result.events
        statuses.append(result.status)
        cursor = result.next_cursor
        if not result.has_more:
            return events, cursor, statuses
    raise AssertionError("never finished: has_more stayed true")


@pytest.fixture
def log(tmp_path):
    return tmp_path / "app.log"


def test_a_second_read_returns_only_what_was_appended(log):
    log.write_text(ts(1) + ts(2) + ts(3))
    first = reader(log).read_batch()
    with log.open("a") as f:
        f.write(ts(4) + ts(5))

    second = reader(log).read_batch(first.next_cursor)

    assert [e.event.text for e in second.events] == [ts(4).strip(), ts(5).strip()]
    assert second.events[0].event.start_offset == first.next_cursor.offset and second.status == "ok"


def test_a_trace_still_being_written_is_never_split(log):
    log.write_text(ts(1, "ERROR boom") + "\n" + FRAMES)
    held = reader(log, HOLD).read_batch()
    assert held.events == () and held.next_cursor.offset == 0

    with log.open("a") as f:
        f.write("\tat d.E.f(E.java:2)\n" + ts(2))
    done = reader(log, HOLD).read_batch(held.next_cursor)

    (event,) = done.events
    assert event.event.text.count("\tat ") == 2 and event.event.text.startswith("2026-10-29T10:00:01Z ERROR boom")
    assert done.next_cursor.offset == log.read_bytes().rindex(b"2026-10-29T10:00:02Z")


def test_a_quiet_file_flushes_its_last_event(log):
    log.write_text(ts(1, "ERROR boom") + FRAMES)

    result = reader(log, settle=0).read_batch()

    assert len(result.events) == 1 and result.next_cursor.offset == log.stat().st_size


def test_a_half_written_last_line_is_not_taken_until_it_is_finished(log):
    log.write_text(ts(1) + ts(2)[:-1])
    held = reader(log, HOLD).read_batch()
    assert held.events == () and held.next_cursor.offset == 0

    with log.open("a") as f:
        f.write("\n" + ts(3))
    done = reader(log, HOLD).read_batch(held.next_cursor)

    assert [e.event.text for e in done.events] == [ts(1).strip(), ts(2).strip()]


def test_a_final_line_without_a_newline_is_taken_once_the_file_is_quiet(log):
    log.write_text(ts(1) + ts(2).rstrip("\n"))

    result = reader(log, settle=0).read_batch()

    assert [e.event.text for e in result.events] == [ts(1).strip(), ts(2).strip()]


def test_a_truncated_file_restarts_from_the_beginning(log):
    log.write_text("".join(ts(n) for n in range(5)))
    first = reader(log).read_batch()
    log.write_text(ts(10, "WARN fresh") + ts(11, "WARN fresher"))

    second = reader(log).read_batch(first.next_cursor)

    assert second.status == "rotated" and [e.event.text.split(" ", 1)[1] for e in second.events] == ["WARN fresh", "WARN fresher"]
    assert second.events[0].event.start_offset == 0 and second.next_cursor.offset == log.stat().st_size


def test_a_rewritten_larger_file_is_noticed_by_its_start(log):
    log.write_text("".join(ts(n) for n in range(5)))
    first = reader(log).read_batch()
    log.write_text("".join(ts(n, "ERROR different") for n in range(20)))  # same path, bigger, other content

    second = reader(log).read_batch(first.next_cursor)

    assert second.status == "rotated" and len(second.events) == 20 and second.events[0].event.start_offset == 0


def test_a_replaced_file_is_noticed_even_if_it_starts_the_same(tmp_path, log):
    log.write_text("".join(ts(n) for n in range(5)))
    first = reader(log).read_batch()
    replacement = tmp_path / "new.log"
    replacement.write_text("".join(ts(n) for n in range(8)))
    os.replace(replacement, log)

    second = reader(log).read_batch(first.next_cursor)

    assert second.status == "rotated" and len(second.events) == 8


def test_a_file_that_only_grew_is_not_mistaken_for_a_new_one(log):
    log.write_text(ts(1))  # smaller than the 256-byte fingerprint
    first = reader(log).read_batch()
    with log.open("a") as f:
        f.write("".join(ts(n) for n in range(2, 40)))

    second = reader(log).read_batch(first.next_cursor)

    assert second.status == "ok" and len(second.events) == 38


def test_a_missing_file_reports_it_and_keeps_the_cursor(log):
    cursor = Cursor(offset=100, line=7, head_len=4, head_sha="x", file_id="1:2")

    result = reader(log).read_batch(cursor)

    assert result.status == "missing" and result.events == () and result.next_cursor == cursor
    log.write_text(ts(1))
    assert len(reader(log).read_batch(Cursor()).events) == 1


def test_a_path_that_cannot_be_read_is_an_error_not_a_crash(tmp_path):
    result = reader(tmp_path).read_batch()  # a directory

    assert result.status == "error" and result.error and result.events == () and result.next_cursor == Cursor()


def test_reading_in_small_batches_gives_exactly_the_events_of_one_pass(log):
    rng = random.Random(7)
    parts = []
    for n in range(300):
        parts.append(ts(n % 3600, "ERROR x" if n % 3 == 0 else "INFO y"))
        if n % 3 == 0:
            parts.append("\n" + FRAMES * rng.randint(1, 6) + "Caused by: java.io.IOException: z\n" + FRAMES)
    log.write_text("".join(parts))

    single, *_ = read_all(reader(log))
    by_events, *_ = read_all(reader(log), max_events=7)
    by_bytes, *_ = read_all(reader(log, max_read_bytes=2000), max_events=1000)
    both, *_ = read_all(reader(log, max_read_bytes=700), max_events=5)

    keys = [e.event_key for e in single]
    assert len(keys) == 300 and len(set(keys)) == 300
    assert [e.event_key for e in by_events] == keys and [e.event_key for e in by_bytes] == keys
    assert [e.event_key for e in both] == keys
    assert [e.event.text for e in both] == [e.event.text for e in single]


def test_byte_offsets_reproduce_the_original_bytes_even_with_crlf(log):
    log.write_bytes(b"".join(ts(n, "ERROR a\n\tat x.Y.z(Y.java:1)").replace("\n", "\r\n").encode() for n in range(6)))
    data = log.read_bytes()

    events, *_ = read_all(reader(log))

    assert len(events) == 6
    for e in events:
        raw = data[e.event.start_offset:e.event.end_offset]
        assert raw.decode().replace("\r\n", "\n").rstrip("\n") == e.event.text
        assert e.locator["sha256"] == hashlib.sha256(raw).hexdigest()


def test_different_bytes_at_the_same_offset_get_different_keys(log):
    log.write_text(ts(1, "INFO old"))
    old = reader(log).read_batch().events[0]
    log.write_text(ts(1, "INFO new"))
    new = reader(log).read_batch().events[0]

    assert old.event.start_offset == new.event.start_offset == 0 and old.event_key != new.event_key


def test_a_writer_appending_while_we_read_loses_and_duplicates_nothing(log):
    log.write_text("")
    done = threading.Event()

    def write():
        with log.open("a", buffering=1) as f:
            for n in range(200):
                f.write(ts(n % 3600, f"INFO event-{n}"))
                if n % 20 == 0:
                    time.sleep(0.005)
        done.set()

    writer = threading.Thread(target=write)
    writer.start()
    collected, cursor = [], Cursor()
    while not done.is_set():
        result = reader(log, HOLD).read_batch(cursor)
        collected += result.events
        cursor = result.next_cursor
    writer.join()
    collected += read_all(reader(log, 0), cursor)[0]  # the file is quiet now: flush the held-back event

    assert [e.event.text.split()[-1] for e in collected] == [f"event-{n}" for n in range(200)]


def test_a_utf8_bom_is_skipped_and_utf16_is_refused(tmp_path):
    bom, utf16 = tmp_path / "bom.log", tmp_path / "u16.log"
    bom.write_bytes(b"\xef\xbb\xbf" + (ts(1) + ts(2)).encode())
    utf16.write_text(ts(1), encoding="utf-16")

    events, *_ = read_all(reader(bom))
    refused = reader(utf16).read_batch()

    assert [e.event.text for e in events] == [ts(1).strip(), ts(2).strip()] and events[0].event.start_offset == 3
    assert refused.status == "error" and refused.error == "UnsupportedEncoding"


def test_json_logs_make_each_line_an_event(log):
    log.write_text('{"a":1}\n{"a":2}\n{"a":3}\n')

    events, *_ = read_all(reader(log, fmt="json"))

    assert [e.event.text for e in events] == ['{"a":1}', '{"a":2}', '{"a":3}']


def test_one_enormous_line_is_clipped_and_does_not_stall_reading(log):
    log.write_text(ts(1) + "x" * 3_000_000 + "\n" + ts(2))

    events, cursor, _ = read_all(reader(log, max_read_bytes=1_000_000))

    assert cursor.offset == log.stat().st_size and any(e.event.truncated for e in events)
    assert events[-1].event.text == ts(2).strip()


def test_cursors_survive_json_and_damaged_ones_start_over(caplog):
    cursor = Cursor(offset=10, line=3, head_len=10, head_sha="ab", file_id="1:2")

    assert Cursor.from_json(cursor.to_json()) == cursor
    for damaged in (None, "", "not json", "[]", '{"offset": -1}', '{"offset":"1","line":1,"head_len":0,"head_sha":"","file_id":""}'):
        assert Cursor.from_json(damaged) == Cursor()
    assert "unreadable source cursor" in caplog.text
