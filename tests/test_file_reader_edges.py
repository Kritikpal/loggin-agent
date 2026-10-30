from tests.test_file_reader import FRAMES, HOLD, read_all, reader, ts


def test_a_file_of_only_blank_lines_yields_nothing_and_keeps_the_cursor_at_the_start(tmp_path):
    log = tmp_path / "blank.log"
    log.write_text("\n\n   \n")

    result = reader(log, HOLD).read_batch()

    assert result.events == () and result.next_cursor.offset == 0 and not result.has_more


def test_one_event_bigger_than_the_read_buffer_is_emitted_instead_of_stalling_forever(tmp_path):
    log = tmp_path / "big.log"
    log.write_text(ts(1, "ERROR big") + FRAMES * 60 + ts(2) + ts(3))  # the first event is ~1.6 KB

    events, cursor, _ = read_all(reader(log, HOLD, max_read_bytes=500))  # still writing: the newest event is held back

    assert len(events) >= 1 and events[0].event.text.startswith("2026-10-29T10:00:01Z ERROR big")
    assert all(a.event.end_offset <= b.event.start_offset for a, b in zip(events, events[1:]))
