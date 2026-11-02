import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.ingestion.assembler import DEFAULT_EVENT_START, assemble
from app.ingestion.fields import Provenance, ResolvedEvent, resolve
from app.ingestion.redaction import redact
from app.ingestion.semantic_text import ParsedEvent, build_semantic_text
from app.ingestion.sources import FileSource
from tests.test_assembler import lines_of
from tests.test_semantic_text import FIXTURES

TEXT = FileSource(id="text", path=Path("app.log"))
CANONICAL = ("level", "logger", "message", "exception_type", "exception_message", "trace_id", "request_id")


def json_source(**kwargs) -> FileSource:
    return FileSource(id="json", path=Path("app.log"), format="json", **kwargs)


def resolve_json(obj: object, **kwargs) -> ResolvedEvent:
    return resolve(json.dumps(obj), json_source(**kwargs))


def assert_provenance_complete(result: ResolvedEvent) -> None:
    for name in CANONICAL:
        if getattr(result.parsed, name) is not None:
            assert name in result.provenance, name
    if result.occurred_at is not None:
        assert "occurred_at" in result.provenance
        assert result.occurred_at.utcoffset() == timedelta(0)


# --- the real Spring Boot fixture -------------------------------------------------------------------------------------


def spring_results() -> list[ResolvedEvent]:
    events = assemble(lines_of((FIXTURES / "spring_boot.log").read_bytes()), DEFAULT_EVENT_START)
    return [resolve(redact(event.text).text, TEXT) for event in events]


# --- JSON samples -------------------------------------------------------------------------------------------------------


def test_an_ecs_document_resolves_its_dotted_and_nested_fields():
    result = resolve_json({
        "@timestamp": "2026-10-29T08:16:05.893Z", "log.level": "error", "message": "Checkout failed",
        "error": {"type": "java.lang.NullPointerException", "message": "cart is null",
                  "stack_trace": "java.lang.NullPointerException: cart is null\n\tat com.acme.A.b(A.java:1)"},
        "trace": {"id": "4bf92f3577b34da6"}, "service": {"name": "orders"}, "http": {"response": {"status_code": 500}},
    })

    parsed = result.parsed
    assert result.occurred_at == datetime(2026, 10, 29, 8, 16, 5, 893000, tzinfo=UTC)
    assert (parsed.level, parsed.message, parsed.logger) == ("ERROR", "Checkout failed", None)  # service.name is no logger
    assert (parsed.exception_type, parsed.exception_message) == ("java.lang.NullPointerException", "cart is null")
    assert parsed.stack_trace.endswith("(A.java:1)") and parsed.trace_id == "4bf92f3577b34da6"
    assert result.provenance["level"] == Provenance("name", 0.9, "log.level", None)
    assert result.fields == {"service": {"name": "orders"}, "http": {"response": {"status_code": 500}}}
    assert parsed.extra == {"http.response.status_code": 500, "service.name": "orders"}
    assert_provenance_complete(result)


def test_a_logback_json_event_with_an_mdc_and_an_offset():
    result = resolve_json({
        "@timestamp": "2026-10-29T08:16:05.893+05:30", "@version": "1", "message": "Checkout failed",
        "logger_name": "com.acme.OrderService", "thread_name": "nio-8080-exec-3", "level": "ERROR",
        "level_value": 40000, "stack_trace": "java.lang.IllegalStateException: x\n\tat com.acme.A.b(A.java:1)",
        "mdc": {"traceId": "abc123", "requestId": "r-9", "user": "u1"},
    })

    parsed = result.parsed
    assert result.occurred_at == datetime(2026, 10, 29, 2, 46, 5, 893000, tzinfo=UTC)
    assert result.provenance["occurred_at"].note is None
    assert (parsed.logger, parsed.level, parsed.trace_id, parsed.request_id) == (
        "com.acme.OrderService", "ERROR", "abc123", "r-9")
    assert result.provenance["trace_id"] == Provenance("name", 0.9, "mdc.traceId", None)
    assert parsed.stack_trace.startswith("java.lang.IllegalStateException")
    assert result.fields == {"@version": "1", "level_value": 40000, "mdc": {"user": "u1"}, "thread_name": "nio-8080-exec-3"}


def test_a_python_json_logger_record():
    result = resolve_json({
        "asctime": "2026-10-29 08:15:23,412", "levelname": "WARNING", "name": "app.db", "message": "slow query",
        "duration_ms": 1234,
    })

    assert result.occurred_at == datetime(2026, 10, 29, 8, 15, 23, 412000, tzinfo=UTC)
    assert result.provenance["occurred_at"] == Provenance("name", 0.9, "asctime", "tz_assumed")
    assert (result.parsed.level, result.parsed.logger, result.parsed.message) == ("WARN", "app.db", "slow query")
    assert result.fields == {"duration_ms": 1234} and result.parsed.extra == {"duration_ms": 1234}


def test_a_pino_record_with_a_numeric_level_epoch_millis_and_an_err_object():
    when = datetime(2026, 10, 29, 8, 16, 5, 893000, tzinfo=UTC)
    millis = (when - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)

    result = resolve_json({
        "level": 50, "time": millis, "pid": 42, "hostname": "web-1", "msg": "boom",
        "err": {"type": "TypeError", "message": "x is undefined", "stack": "TypeError: x is undefined\n    at f (a.js:1:1)"},
    })

    assert result.parsed.level == "ERROR"
    assert result.provenance["level"] == Provenance("value", 0.6, "level", "numeric_level")
    assert result.occurred_at == when and result.provenance["occurred_at"].note == "epoch_millis"
    assert (result.parsed.exception_type, result.parsed.message) == ("TypeError", "boom")
    assert result.parsed.stack_trace.startswith("TypeError: x is undefined")
    assert result.fields == {"hostname": "web-1", "pid": 42}


def test_a_bunyan_record_with_epoch_seconds():
    result = resolve_json({"v": 0, "level": 30, "name": "api", "ts": 1793261723, "msg": "listening"})

    assert result.parsed.level == "INFO" and result.parsed.logger == "api"
    assert result.occurred_at == datetime.fromtimestamp(1793261723, UTC)
    assert result.provenance["occurred_at"] == Provenance("name", 0.9, "ts", "epoch_seconds")


def test_an_exception_string_is_split_into_type_and_message_and_kept_as_the_trace():
    result = resolve_json({"msg": "failed", "exception": "java.io.IOException: disk full\n\tat a.B.c(B.java:1)"})

    parsed = result.parsed
    assert (parsed.exception_type, parsed.exception_message) == ("java.io.IOException", "disk full")
    assert parsed.stack_trace.endswith("(B.java:1)") and result.fields == {}
    assert result.provenance["exception_type"] == Provenance("name", 0.9, "exception", "split")


def test_nested_trace_and_request_ids_in_a_context_object_and_case_insensitive_keys():
    result = resolve_json({"MESSAGE": "hi", "Context": {"Trace-Id": "t-1", "Correlation_ID": "c-2"}})

    assert (result.parsed.message, result.parsed.trace_id, result.parsed.request_id) == ("hi", "t-1", "c-2")
    assert result.provenance["request_id"].source == "Context.Correlation_ID"


# --- configured field mappings ------------------------------------------------------------------------------------------


def test_a_mapping_beats_name_recognition_and_the_unused_alias_stays_in_fields():
    result = resolve_json(
        {"message": "generic", "payload": {"description": "specific"}},
        field_mappings={"message": "payload.description"},
    )

    assert result.parsed.message == "specific"
    assert result.provenance["message"] == Provenance("mapping", 1.0, "payload.description", None)
    assert result.fields == {"message": "generic"}


def test_a_mapping_reaches_flat_dotted_keys_and_ignores_case():
    result = resolve_json({"Payload.Description": "flat", "level": "INFO", "sev": "error"},
                          field_mappings={"message": "payload.description", "level": "SEV"})

    assert (result.parsed.message, result.parsed.level) == ("flat", "ERROR")
    assert result.provenance["level"].method == "mapping" and result.fields == {"level": "INFO"}


def test_a_missing_or_unusable_mapped_key_falls_through_to_name_recognition():
    result = resolve_json({"message": "by name", "lvl": "verbose", "level": "warn"},
                          field_mappings={"message": "payload.missing", "level": "lvl", "colour": "c"})

    assert result.parsed.message == "by name" and result.provenance["message"].method == "name"
    assert result.parsed.level == "WARN" and result.fields == {"lvl": "verbose"}


def test_a_mapped_numeric_level_and_epoch_are_accepted():
    result = resolve_json({"sev": 40, "when": 1793261723}, field_mappings={"level": "sev", "occurred_at": "when"})

    assert result.parsed.level == "WARN" and result.provenance["level"] == Provenance("mapping", 1.0, "sev", "numeric_level")
    assert result.occurred_at == datetime.fromtimestamp(1793261723, UTC)


# --- no evidence, no fact -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "an error occurred while saving the order",
    "2026-10-29T08:15:23Z something error happened, failed and fatal",
    "2026-10-29T08:15:23Z " + "x" * 80 + " ERROR far too late on the line",
    "java.lang.IllegalStateException: boom",
    "ERRORS were INFOrmational",
])
def test_text_without_level_evidence_has_no_level(text):
    result = resolve(text, TEXT)

    assert result.parsed.level is None and "level" not in result.provenance
    assert result.parsed.message is not None


def test_an_exception_name_does_not_imply_a_level():
    result = resolve("java.lang.IllegalStateException: boom\n\tat a.B.c(B.java:1)", TEXT)

    assert result.parsed.exception_type == "java.lang.IllegalStateException"
    assert result.parsed.exception_message == "boom" and result.parsed.level is None


@pytest.mark.parametrize("obj", [
    {"message": "an error occurred"},
    {"level": 3}, {"level": 35}, {"level": True}, {"level": 30.0}, {"code": 50}, {"level": "verbose"},
    {"level": ""}, {"status": 40},
])
def test_json_without_level_evidence_has_no_level_and_keeps_the_raw_value(obj):
    result = resolve_json(obj)

    assert result.parsed.level is None and "level" not in result.provenance
    assert {k: v for k, v in obj.items() if k != "message"} == result.fields


@pytest.mark.parametrize("obj", [
    {"timestamp": "yesterday"}, {"timestamp": "2026-13-45T99:99:99Z"}, {"time": "1999-12-31T23:59:59Z"},
    {"time": "2026-10-29"}, {"time": 12345}, {"time": 9_999_999_999_999}, {"time": True}, {"duration": 1793261723},
    {"time": "2026-10-29T08:15:23+25:00"}, {"t": "2026-10-29T08:15:23Z trailing"}, {"time": "Infinity"},
])
def test_odd_timestamps_and_arbitrary_numbers_give_no_time(obj):
    result = resolve_json(obj)

    assert result.occurred_at is None and "occurred_at" not in result.provenance
    assert result.fields == obj


def test_an_error_string_that_is_not_an_exception_is_left_alone():
    result = resolve_json({"error": "connection refused", "message": "retrying"})

    assert result.parsed.exception_type is None and result.fields == {"error": "connection refused"}


# --- levels and timestamps -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "level"), [
    ("WARNING", "WARN"), ("warning", "WARN"), ("Warn", "WARN"), ("SEVERE", "ERROR"), ("err", "ERROR"),
    ("CRITICAL", "FATAL"), ("Emergency", "FATAL"), ("emerg", "FATAL"), ("alert", "FATAL"), ("panic", "FATAL"),
    ("NOTICE", "INFO"), (" info ", "INFO"), ("FINE", "DEBUG"), ("finer", "TRACE"), ("FINEST", "TRACE"),
    ("trace", "TRACE"), ("debug", "DEBUG"), ("fatal", "FATAL"), ("junk", None), ("CONFIG", None),
])
def test_json_levels_are_normalised(raw, level):
    assert resolve_json({"level": raw}).parsed.level == level


@pytest.mark.parametrize(("line", "level"), [
    ("2026-10-29 08:15:23,412 [Warn] disk almost full", "WARN"),
    ("[error] [client 10.0.0.1] File does not exist", "ERROR"),
    ("WARNING:root:disk low", "WARN"),
    ("12:00 main | SEVERE | pool exhausted", "ERROR"),
    ("2026-10-29T08:15:23Z [main] CRITICAL c.a.B - down", "FATAL"),
    ("2026-10-29T08:15:23Z [notice] started", "INFO"),
])
def test_text_level_tokens(line, level):
    result = resolve(line, TEXT)

    assert result.parsed.level == level and result.provenance["level"].method == "value"


def test_a_naive_timestamp_is_read_in_the_source_timezone():
    kolkata = FileSource(id="text", path=Path("app.log"), timezone="Asia/Kolkata")

    text = resolve("2026-10-29 08:15:23,412 INFO started", kolkata)
    from_json = resolve('{"timestamp": "2026-10-29T08:15:23"}', json_source(timezone="Asia/Kolkata"))

    assert text.occurred_at == datetime(2026, 10, 29, 2, 45, 23, 412000, tzinfo=UTC)
    assert text.provenance["occurred_at"].note == "tz_assumed" and text.parsed.message == "started"
    assert from_json.occurred_at == datetime(2026, 10, 29, 2, 45, 23, tzinfo=UTC)
    assert from_json.provenance["occurred_at"] == Provenance("name", 0.9, "timestamp", "tz_assumed")


@pytest.mark.parametrize(("stamp", "expected"), [
    ("2026-10-29T08:15:23Z", datetime(2026, 10, 29, 8, 15, 23, tzinfo=UTC)),
    ("2026-10-29t08:15:23z", datetime(2026, 10, 29, 8, 15, 23, tzinfo=UTC)),
    ("2026-10-29T08:15:23+05:30", datetime(2026, 10, 29, 2, 45, 23, tzinfo=UTC)),
    ("2026-10-29T08:15:23+0530", datetime(2026, 10, 29, 2, 45, 23, tzinfo=UTC)),
    ("2026-10-29T08:15:23-05:00", datetime(2026, 10, 29, 13, 15, 23, tzinfo=UTC)),
    ("2026-10-29 08:15:23 +0000", datetime(2026, 10, 29, 8, 15, 23, tzinfo=UTC)),
    ("2026-10-29T08:15:23,5Z", datetime(2026, 10, 29, 8, 15, 23, 500000, tzinfo=UTC)),
    ("2026-10-29T08:15:23.123456789Z", datetime(2026, 10, 29, 8, 15, 23, 123456, tzinfo=UTC)),
    ("2026-10-29T08:15Z", datetime(2026, 10, 29, 8, 15, tzinfo=UTC)),
])
def test_iso_timestamps_in_json_and_at_the_start_of_text(stamp, expected):
    assert resolve_json({"time": stamp}).occurred_at == expected
    assert resolve(f"{stamp} INFO ok", TEXT).occurred_at == expected


def test_a_timestamp_not_at_the_start_of_the_text_is_ignored():
    result = resolve("started at 2026-10-29T08:15:23Z", TEXT)

    assert result.occurred_at is None and result.parsed.message == "started at 2026-10-29T08:15:23Z"


# --- text details --------------------------------------------------------------------------------------------------------


def test_text_trace_and_request_ids_come_only_from_explicit_pairs():
    result = resolve("2026-10-29T08:15:23Z INFO done traceId=4bf92f35 x-request-id: \"r-9\" user=a", TEXT)
    plain = resolve("2026-10-29T08:15:23Z INFO the trace was lost, request failed", TEXT)

    assert (result.parsed.trace_id, result.parsed.request_id) == ("4bf92f35", "r-9")
    assert result.provenance["trace_id"] == Provenance("name", 0.9, "text", None)
    assert (plain.parsed.trace_id, plain.parsed.request_id) == (None, None)


def test_a_level_in_the_middle_of_the_first_line_is_cut_out_of_the_message():
    result = resolve("2026-10-29T08:15:23Z [main] ERROR c.a.B: boom", TEXT)  # no " - ": not the Logback layout

    assert result.parsed.message == "[main] c.a.B: boom"


def test_text_with_nothing_recognised_keeps_its_first_line_as_the_message():
    result = resolve("just some words\nand a second line", TEXT)

    assert result.parsed == ParsedEvent(raw_text="just some words\nand a second line", message="just some words")
    assert result.provenance == {"message": Provenance("value", 0.6, "text", None)}
    assert result.fields == {} and result.occurred_at is None


# --- determinism -------------------------------------------------------------------------------------------------------


def test_the_same_input_gives_the_same_result():
    text = '{"level": "info", "msg": "x", "a": {"b": [1, 2, {"c": null}]}}'

    assert resolve(text, json_source()) == resolve(text, json_source())
    assert resolve("2026-10-29T08:15:23Z INFO x", TEXT) == resolve("2026-10-29T08:15:23Z INFO x", TEXT)


def test_json_key_order_does_not_change_the_result():
    obj = {"Level": "info", "level": "error", "msg": "x", "message": "y", "z": 1, "a": {"q": 1, "p": 2},
           "trace.id": "flat", "trace": {"id": "nested"}}
    backwards = dict(reversed(obj.items()))

    first, second = resolve_json(obj), resolve_json(backwards)

    assert replace(first.parsed, raw_text="") == replace(second.parsed, raw_text="")
    assert (first.occurred_at, first.provenance, first.fields) == (second.occurred_at, second.provenance, second.fields)
    assert list(first.fields) == list(second.fields)
