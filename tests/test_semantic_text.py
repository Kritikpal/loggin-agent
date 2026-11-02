import time
import re
from pathlib import Path

from app.ingestion.semantic_text import (
    MAX_MESSAGE_CHARS, MAX_RAW_FALLBACK_CHARS, MAX_TEXT_CHARS, ParsedEvent, build_semantic_text,
)


def test_all_parts_appear_in_priority_order():
    event = ParsedEvent(
        raw_text="raw", level="ERROR", logger="payment-service", message="Connection to db timed out",
        trace_id="abc123", request_id="r-9", exception_type="SQLTimeoutException", exception_message="too slow",
        extra={"endpoint": "/pay", "status": "504"},
    )

    result = build_semantic_text(event)

    assert result.text.split("\n") == [
        "ERROR payment-service", "Connection to db timed out", "trace_id=abc123 request_id=r-9",
        "SQLTimeoutException: too slow", "endpoint=/pay", "status=504",
    ]
    assert not result.truncated


def test_a_plain_message_gives_just_the_message():
    assert build_semantic_text(ParsedEvent(raw_text="raw", message="disk full")).text == "disk full"


def test_missing_and_blank_parts_leave_no_gaps_or_none():
    event = ParsedEvent(raw_text="raw", level="WARN", message="  ", exception_type="IOError", extra={"a": " ", "b": "x"})

    text = build_semantic_text(event).text

    assert text == "WARN\nIOError\nb=x" and "None" not in text


def test_raw_text_is_used_when_nothing_was_parsed():
    result = build_semantic_text(ParsedEvent(raw_text="2026-10-29 weird unparsable line", level="ERROR"))

    assert result.text == "ERROR\n2026-10-29 weird unparsable line"


def test_long_raw_fallback_and_message_are_clipped_and_flagged():
    raw = build_semantic_text(ParsedEvent(raw_text="x" * 5000))
    msg = build_semantic_text(ParsedEvent(raw_text="r", message="y" * 1000))

    assert len(raw.text) == MAX_RAW_FALLBACK_CHARS and raw.truncated
    assert len(msg.text) == MAX_MESSAGE_CHARS and msg.truncated


def test_same_event_gives_same_text():
    event = ParsedEvent(raw_text="r", message="m", extra={"k": "v"})

    assert build_semantic_text(event) == build_semantic_text(event)


def test_clipped_extra_values_and_dropped_extra_fields_are_flagged():
    long_value = build_semantic_text(ParsedEvent(raw_text="r", message="m", extra={"k": "v" * 500}))
    many = build_semantic_text(ParsedEvent(raw_text="r", message="m", extra={str(i): "v" for i in range(8)}))

    assert long_value.truncated and len(long_value.text.split("\n")[1]) == 2 + 100
    assert many.truncated and len(many.text.split("\n")) == 1 + 5


def test_message_exactly_at_the_limit_is_not_truncated_but_one_more_is():
    at_limit = build_semantic_text(ParsedEvent(raw_text="r", message="m" * MAX_MESSAGE_CHARS))
    over = build_semantic_text(ParsedEvent(raw_text="r", message="m" * (MAX_MESSAGE_CHARS + 1)))

    assert not at_limit.truncated and len(at_limit.text) == MAX_MESSAGE_CHARS
    assert over.truncated and len(over.text) == MAX_MESSAGE_CHARS


def test_non_ascii_text_is_kept_intact():
    event = ParsedEvent(raw_text="r", logger="zahlungs-dienst", message="Ünïcode — 日本語 ошибка", extra={"город": "Köln"})

    assert build_semantic_text(event).text == "zahlungs-dienst\nÜnïcode — 日本語 ошибка\nгород=Köln"


def test_a_multiline_message_does_not_crash_and_keeps_its_content():
    text = build_semantic_text(ParsedEvent(raw_text="r", message="line one\nline two")).text

    assert "line one" in text and "line two" in text


def test_text_never_exceeds_the_total_cap():
    event = ParsedEvent(
        raw_text="r", level="ERROR", logger="l" * 400, message="m" * 400, trace_id="t" * 400,
        exception_type="E" * 300, extra={f"key{i}": "v" * 100 for i in range(5)},
    )

    assert len(build_semantic_text(event).text) <= MAX_TEXT_CHARS


FIXTURES = Path(__file__).parent / "fixtures"
NEW_EVENT = re.compile(r"^\d{4}-\d\d-\d\dT")  # minimal stand-in for the step-5 multiline assembler


def spring_events() -> list[str]:
    events: list[str] = []
    for line in (FIXTURES / "spring_boot.log").read_text().splitlines():
        if NEW_EVENT.match(line) or not events:
            events.append(line)
        else:
            events[-1] += "\n" + line
    return events


def spring_npe() -> ParsedEvent:
    raw = next(e for e in spring_events() if "NullPointerException" in e)
    head, _, trace = raw.partition("\n\n")
    return ParsedEvent(raw_text=raw, level="ERROR", logger="dispatcherServlet", message=head.split(" : ", 1)[1], stack_trace=trace)


NESTED = """org.springframework.web.client.ResourceAccessException: I/O error on POST request
\tat com.acme.PayClient.charge(PayClient.java:10)
Caused by: java.net.SocketTimeoutException: Read timed out
\tat com.acme.net.Http.send(Http.java:99)
\tat com.acme.PayClient.charge(PayClient.java:8)
... 12 more"""


def test_root_cause_and_its_frames_are_used_for_a_nested_trace():
    lines = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=NESTED)).text.split("\n")

    assert lines == [
        "org.springframework.web.client.ResourceAccessException: I/O error on POST request",
        "root cause: java.net.SocketTimeoutException: Read timed out",
        "at com.acme.net.Http.send(Http.java:99)",
        "at com.acme.PayClient.charge(PayClient.java:8)",
    ]


def test_a_huge_trace_keeps_five_frames_and_the_root_cause_and_is_flagged():
    frames = "\n".join(f"\tat com.acme.Deep.f{i}(Deep.java:{i})" for i in range(500))
    trace = f"java.lang.IllegalStateException: boom\nCaused by: java.io.IOException: disk\n{frames}"

    result = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace))

    assert result.truncated and "root cause: java.io.IOException: disk" in result.text
    assert result.text.count("\nat ") == 5


def test_a_trace_made_only_of_library_frames_still_shows_some_frames():
    trace = "java.lang.Error: x\n\tat org.springframework.Foo.bar(Foo.java:1)\n\tat java.base/java.lang.Thread.run(Thread.java:1)"

    text = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace)).text

    assert "at org.springframework.Foo.bar(Foo.java:1)" in text and "at java.lang.Thread.run(Thread.java:1)" in text


def test_explicit_exception_fields_win_over_the_trace_header():
    event = ParsedEvent(raw_text="r", exception_type="Custom", exception_message="m", stack_trace=NESTED)

    assert build_semantic_text(event).text.split("\n")[0] == "Custom: m"


def oversized_core(**overrides) -> ParsedEvent:
    """Every core part at its cap: 400 + 400 + 399 + 300 + 300 chars plus 4 newlines, just over the total cap."""
    fields = dict(
        raw_text="r", logger="l" * 400, message="m" * 400, trace_id="t" * 390,
        exception_type="E" * 300, stack_trace="Caused by: " + "R" * 500,
    )
    return ParsedEvent(**{**fields, **overrides})


def test_the_root_cause_survives_an_oversized_event_while_frames_are_dropped():
    frames = "\n".join(f"\tat com.acme.F{i}.run(F.java:{i})" for i in range(3))
    result = build_semantic_text(oversized_core(stack_trace="Caused by: " + "R" * 500 + "\n" + frames))

    lines = result.text.split("\n")
    assert len(result.text) <= MAX_TEXT_CHARS and result.truncated
    assert lines[-1].startswith("root cause: RRR") and not any(line.startswith("at ") for line in lines)


def test_optional_parts_are_dropped_whole_never_cut_in_half():
    frames = "\n".join(f"\tat com.acme.{'x' * 120}{i}.run(F.java:{i})" for i in range(5))
    event = oversized_core(
        logger=None, trace_id="t" * 391, stack_trace="Caused by: " + "R" * 500 + "\n" + frames, extra={"k": "v"},
    )

    result = build_semantic_text(event)

    kept_frames = [line for line in result.text.split("\n") if line.startswith("at ")]
    assert 1 <= len(kept_frames) < 5 and all(line.endswith(")") for line in kept_frames)
    assert "k=v" not in result.text and len(result.text) <= MAX_TEXT_CHARS and result.truncated


def test_an_event_within_the_budget_is_not_flagged():
    assert not build_semantic_text(ParsedEvent(raw_text="r", message="small", extra={"k": "v"})).truncated


def test_a_pathological_whitespace_line_is_handled_quickly():
    trace = "E: a\n\tat x" + " " * 30000 + "y"

    started = time.perf_counter()
    build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace))

    assert time.perf_counter() - started < 2


def test_a_blank_message_without_exception_falls_back_to_raw_text():
    assert build_semantic_text(ParsedEvent(raw_text="RAW", level="ERROR", message="  ")).text == "ERROR\nRAW"
    assert build_semantic_text(ParsedEvent(raw_text="RAW", exception_type="  ")).text == "RAW"


def test_extra_values_of_any_json_type_do_not_crash():
    event = ParsedEvent(raw_text="r", message="m", extra={"status": 504, "ok": False, "gone": None, "name": "x"})

    assert build_semantic_text(event).text == "m\ngone=None".replace("gone=None", "name=x\nok=False\nstatus=504")


def test_extra_fields_give_the_same_text_whatever_their_order():
    one = ParsedEvent(raw_text="r", message="m", extra={"a": "1", "b": "2"})
    two = ParsedEvent(raw_text="r", message="m", extra={"b": "2", "a": "1"})

    assert build_semantic_text(one) == build_semantic_text(two)


def test_a_suppressed_exception_does_not_change_the_root_cause_or_frames():
    trace = (
        "E: a\n\tat com.acme.A.f(A.java:1)\n\tSuppressed: java.io.IOException: s\n"
        "\t\tat com.acme.S.g(S.java:2)\n\t\tCaused by: Z: z\n\tat com.acme.A.h(A.java:3)"
    )

    text = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace)).text

    assert text.split("\n") == ["E: a", "at com.acme.A.f(A.java:1)", "at com.acme.A.h(A.java:3)"]


def test_trace_ids_are_kept_on_the_raw_text_fallback():
    event = ParsedEvent(raw_text="x" * 5000, logger="l" * 400, trace_id="T1")

    result = build_semantic_text(event)

    assert "trace_id=T1" in result.text and len(result.text) <= MAX_TEXT_CHARS and result.truncated


def test_frames_are_cleaned_of_jar_suffixes_with_trailing_spaces_and_odd_module_prefixes():
    trace = "E: a\n\tat com.acme.A.f(A.java:1) ~[a.jar:1]  \n\tat app//com.acme.B.g(B.java:2)\n\tat java.base@17.0.1/java.lang.X.y(X.java:3)"

    text = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace)).text

    assert text.split("\n")[1:] == ["at com.acme.A.f(A.java:1)", "at com.acme.B.g(B.java:2)"]


def test_header_only_and_frames_only_traces_work():
    header_only = build_semantic_text(ParsedEvent(raw_text="r", stack_trace="java.lang.Error: x")).text
    frames_only = build_semantic_text(ParsedEvent(raw_text="r", stack_trace="\tat com.acme.A.f(A.java:1)")).text

    assert header_only == "java.lang.Error: x"
    assert frames_only == "r\nat com.acme.A.f(A.java:1)"  # nothing but frames was parsed, so the raw text stays


def test_prose_starting_with_at_is_not_a_frame():
    trace = "E: a\n\tat least one pool is busy\n\tat com.acme.A.f(A.java:1)"

    text = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace)).text

    assert text.split("\n") == ["E: a", "at com.acme.A.f(A.java:1)"]


def test_all_the_usual_frame_shapes_are_recognised():
    trace = "\n".join([
        "E: a",
        "\tat com.acme.A.<init>(A.java:1)",
        "\tat com.acme.A.lambda$run$0(A.java:2)",
        "\tat com.acme.B$Inner.call(B.java:3) ~[app.jar:1.0]",
        "\tat com.acme.C.nativeCall(Native Method)",
        "\tat com.acme.D.gen(Unknown Source)",
        "\tat app//com.acme.E.run(E.java:5)",
    ])

    lines = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace)).text.split("\n")

    assert lines[1:] == [
        "at com.acme.A.<init>(A.java:1)", "at com.acme.A.lambda$run$0(A.java:2)",
        "at com.acme.B$Inner.call(B.java:3)", "at com.acme.C.nativeCall(Native Method)",
        "at com.acme.D.gen(Unknown Source)",
    ]  # the sixth frame is cut by the 5-frame limit


def trace_of(*frames: str) -> str:
    return "E: a\n" + "\n".join(f"\tat {frame}" for frame in frames)


def test_more_frameworks_are_filtered_by_default():
    trace = trace_of(
        "com.acme.A.f(A.java:1)", "org.hibernate.Session.get(Session.java:2)", "io.netty.Channel.read(Channel.java:3)",
        "com.zaxxer.hikari.Pool.get(Pool.java:4)", "jakarta.servlet.Filter.run(Filter.java:5)",
    )

    result = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace))

    assert result.text.split("\n") == ["E: a", "at com.acme.A.f(A.java:1)"] and result.filtered_frames == 4


def test_named_app_packages_decide_what_is_application_code():
    trace = trace_of(
        "org.springframework.myapp.Job.run(Job.java:1)", "com.acme.Other.run(Other.java:2)",
        "org.springframework.web.Servlet.go(Servlet.java:3)",
    )

    result = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace), app_packages=("org.springframework.myapp",))

    assert result.text.split("\n") == ["E: a", "at org.springframework.myapp.Job.run(Job.java:1)"]
    assert result.filtered_frames == 2


def test_an_app_package_matches_whole_package_names_only():
    trace = trace_of("com.acmecorp.X.f(X.java:1)", "com.acme.Y.g(Y.java:2)")

    result = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace), app_packages=("com.acme",))

    assert result.text.split("\n")[1:] == ["at com.acme.Y.g(Y.java:2)"]


def test_frames_are_still_shown_when_no_frame_matches_the_app_packages_or_all_are_libraries():
    trace = trace_of("org.hibernate.A.f(A.java:1)", "java.lang.B.g(B.java:2)")

    named = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace), app_packages=("com.nowhere",))
    default = build_semantic_text(ParsedEvent(raw_text="r", stack_trace=trace))

    assert named.text == default.text and "at org.hibernate.A.f(A.java:1)" in named.text
    assert named.filtered_frames == default.filtered_frames == 0
