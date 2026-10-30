"""Regression tests for the review of the field resolver."""

import json
import time
from pathlib import Path

import pytest

from app.ingestion.fields import resolve
from app.ingestion.sources import FileSource

TEXT = FileSource(id="t", path=Path("x.log"))
JSON = FileSource(id="j", path=Path("x.log"), format="json")


def test_a_huge_nested_object_cannot_push_the_top_level_fields_out_of_the_budget():
    event = {
        "level": "ERROR", "message": "payment failed", "trace_id": "abc", "@timestamp": "2026-10-29T10:00:00Z",
        "mdc": {f"g{g}": {f"k{i}": i for i in range(60)} for g in range(60)},  # 3,600 values, sorts before "message"
    }

    result = resolve(json.dumps(event), JSON)

    assert (result.parsed.level, result.parsed.message, result.parsed.trace_id) == ("ERROR", "payment failed", "abc")
    assert result.occurred_at is not None and len(json.dumps(result.fields)) < 40_000


def test_a_big_object_before_a_nested_trace_id_does_not_hide_it():
    event = {"a_big": {f"k{i}": i for i in range(3000)}, "trace": {"id": "T1"}, "message": "m"}

    result = resolve(json.dumps(event), JSON)

    assert result.parsed.trace_id == "T1" and result.parsed.message == "m"


def test_cutting_an_oversized_event_is_quick_and_deterministic():
    event = {f"key{i}": {"v": [{"x": j} for j in range(40)]} for i in range(400)}

    started = time.perf_counter()
    first, second = resolve(json.dumps(event), JSON), resolve(json.dumps(dict(reversed(list(event.items())))), JSON)
    elapsed = time.perf_counter() - started

    assert elapsed < 2
    assert first.fields == second.fields and first.parsed.extra == second.parsed.extra  # raw_text differs by design


@pytest.mark.parametrize("line", [
    "2026-10-29T10:00:00Z Connection INFO retrieved for user",
    "2026-10-29T10:00:00Z Payment to ACME failed: ERROR code 5",
    "2026-10-29T10:00:00Z the WARN level is configurable",
])
def test_a_level_word_inside_a_sentence_is_not_a_level(line):
    assert resolve(line, TEXT).parsed.level is None


@pytest.mark.parametrize("line, level", [
    ("2026-10-29T08:15:23.412Z  INFO 14325 --- [main] c.e.Demo : Started", "INFO"),
    ("2026-10-29 08:15:23,412 [http-nio-8080-exec-1] ERROR c.e.Demo - boom", "ERROR"),
    ("2026-10-29T10:00:00Z WARN something", "WARN"),
    ("ERROR something", "ERROR"),
    ("WARNING:root:careful", "WARN"),
    ("2026/10/29 08:15:23 [error] 12#12: upstream timed out", "ERROR"),
    ("12:00 main | SEVERE | pool exhausted", "ERROR"),
    ("2026-10-29T10:00:00Z (worker-2) FATAL out of memory", "FATAL"),
])
def test_real_log_layouts_still_give_their_level(line, level):
    assert resolve(line, TEXT).parsed.level == level


def test_name_is_a_logger_only_inside_a_logging_record():
    record = resolve('{"name":"app.db","levelname":"INFO","message":"up"}', JSON)
    data = resolve('{"name":"Alice","message":"signed up"}', JSON)

    assert record.parsed.logger == "app.db" and "name" not in record.fields
    assert data.parsed.logger is None and data.fields["name"] == "Alice" and data.parsed.extra["name"] == "Alice"
