"""Sub-step 5.6: first-line layouts (Spring Boot, Logback) and splitting the stack trace off a text event."""

import random
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingestion.fields import Provenance, ResolvedEvent, resolve
from app.ingestion.fields.text_layouts import MAX_STACK_TRACE_CHARS
from app.ingestion.semantic_text import build_semantic_text
from app.ingestion.sources import FileSource
from tests.test_field_resolver import assert_provenance_complete, spring_results

TEXT = FileSource(id="text", path=Path("app.log"))
MAX_SECONDS = 2.0
PARSED_BY_LAYOUT = ("occurred_at", "level", "logger", "message")
NPE_HEADER = 'java.lang.NullPointerException: Cannot invoke "com.example.demo.model.Cart.getItems()" because "cart" is null'


def text(value: str) -> ResolvedEvent:
    return resolve(value, TEXT)


# --- the real Spring Boot fixture -------------------------------------------------------------------------------------


def test_every_spring_fixture_event_parses_as_spring_boot():
    results = spring_results()

    assert len(results) == 15
    for result in results:
        assert result.provenance["logger"] == Provenance("parser", 1.0, "spring_boot", None)
        assert result.parsed.level and result.parsed.logger and result.occurred_at is not None
        assert result.fields["pid"] == 14325 and result.fields["thread"]
        assert result.parsed.extra == {}  # pid and thread are layout noise for search; they stay in `fields`
        assert_provenance_complete(result)
    assert results[0].parsed.message == "Starting DemoApplication v1.0.0 using Java 21 on ubuntu-server"
    assert results[0].parsed.logger == "c.e.demo.DemoApplication" and results[0].fields["thread"] == "main"
    assert [r.parsed.stack_trace is not None for r in results] == [False] * 13 + [True, False]


def test_the_spring_npe_event_is_split_into_message_exception_and_trace():
    result = spring_results()[13]
    parsed = result.parsed

    assert parsed.logger == "o.a.c.c.C.[.[.[/].[dispatcherServlet]" and parsed.level == "ERROR"
    assert result.fields == {"pid": 14325, "thread": "nio-8080-exec-3"}
    assert parsed.exception_type == "java.lang.NullPointerException"
    assert parsed.exception_message.startswith("Cannot invoke")
    assert parsed.stack_trace.startswith(NPE_HEADER + "\n\tat com.example.demo.service.OrderService.processCheckout")
    assert parsed.stack_trace.count("\tat ") == 11 and parsed.stack_trace.endswith("~[spring-webmvc-6.1.1.jar:6.1.1]")
    assert parsed.message.startswith("Servlet.service() for servlet [dispatcherServlet]")
    assert parsed.message.endswith("with root cause")
    assert "14325" not in parsed.message and "nio-8080" not in parsed.message and "\tat" not in parsed.message
    for name in ("exception_type", "exception_message", "stack_trace"):
        assert result.provenance[name] == Provenance("parser", 1.0, "spring_boot", None)


def test_the_spring_npe_semantic_text_shows_the_application_frames_only():
    semantic = build_semantic_text(spring_results()[13].parsed, ("com.example",))

    assert "java.lang.NullPointerException" in semantic.text and "OrderService.processCheckout" in semantic.text
    assert "org.springframework" not in semantic.text
    assert "14325" not in semantic.text and "nio-8080" not in semantic.text
    assert semantic.filtered_frames == 9


# --- layout samples ---------------------------------------------------------------------------------------------------


def test_a_logback_default_line():
    result = text("2026-10-29 08:15:23,412 [http-nio-8080-exec-1] ERROR c.e.demo.OrderService - Checkout failed : x --- y")

    parsed = result.parsed
    assert (parsed.level, parsed.logger, parsed.message) == ("ERROR", "c.e.demo.OrderService", "Checkout failed : x --- y")
    assert result.fields == {"thread": "http-nio-8080-exec-1"}
    assert result.occurred_at == datetime(2026, 10, 29, 8, 15, 23, 412000, tzinfo=UTC)
    assert result.provenance["occurred_at"] == Provenance("parser", 1.0, "logback_default", "tz_assumed")
    assert result.provenance["message"] == Provenance("parser", 1.0, "logback_default", None)


@pytest.mark.parametrize(("line", "logger", "thread", "message"), [
    ("2026-10-29T08:15:23.412Z  INFO [myapp,abc123,def456] 1 --- [main] c.e.Demo : Started", "c.e.Demo", "main", "Started"),
    ("2026-10-29T08:15:23.412Z  INFO 1 --- [myapp] [           main] c.e.Demo     : Started", "c.e.Demo", "main", "Started"),
    ("2026-10-29T08:15:23.412Z  WARN 7 --- [pool-1 thread 2] c.e.Demo : a : b --- c", "c.e.Demo", "pool-1 thread 2", "a : b --- c"),
    ("2026-10-29T08:15:23.412Z DEBUG 7 --- [   main] o.a.c.c.C.[Tomcat].[localhost].[/] : Init", "o.a.c.c.C.[Tomcat].[localhost].[/]",
     "main", "Init"),
    ("2026-10-29T08:15:23.412Z TRACE 7 --- [main] c.e.Demo :", "c.e.Demo", "main", None),
], ids=["sleuth-group", "boot-3.2-app-name", "spaces-in-thread", "brackets-in-logger", "empty-message"])
def test_spring_boot_variants(line, logger, thread, message):
    result = text(line)

    assert (result.parsed.logger, result.fields["thread"], result.parsed.message) == (logger, thread, message)
    assert result.provenance["level"].source == "spring_boot"
    assert "message" not in result.provenance or result.provenance["message"].method == "parser"


def test_a_blank_thread_is_left_out_of_the_fields():
    result = text("2026-10-29T08:15:23Z  INFO 1 --- [   ] c.e.Demo : x")

    assert result.fields == {"pid": 1} and result.parsed.logger == "c.e.Demo"


def test_the_application_group_is_not_read_as_trace_ids():
    result = text("2026-10-29T08:15:23.412Z  INFO [myapp,abc123,def456] 1 --- [main] c.e.Demo : Started")

    assert result.parsed.trace_id is None and result.fields == {"pid": 1, "thread": "main"}


def test_a_naive_spring_timestamp_is_read_in_the_source_timezone():
    kolkata = FileSource(id="text", path=Path("app.log"), timezone="Asia/Kolkata")

    result = resolve("2026-10-29 08:15:23.412  INFO 1 --- [main] c.e.Demo : x", kolkata)

    assert result.occurred_at == datetime(2026, 10, 29, 2, 45, 23, 412000, tzinfo=UTC)
    assert result.provenance["occurred_at"] == Provenance("parser", 1.0, "spring_boot", "tz_assumed")


def test_ids_the_layout_does_not_parse_still_come_from_value_recognition():
    result = text("2026-10-29T08:15:23Z  INFO 1 --- [main] c.e.Demo : done traceId=4bf92f35 requestId=r-9")

    assert (result.parsed.trace_id, result.parsed.request_id) == ("4bf92f35", "r-9")
    assert result.provenance["trace_id"] == Provenance("name", 0.9, "text", None)
    assert result.provenance["logger"].method == "parser"


def test_continuation_lines_before_the_trace_belong_to_the_message():
    result = text("2026-10-29T08:15:23Z ERROR 1 --- [main] c.e.Demo : first\n  second\n\nthird  \n\n")

    assert result.parsed.message == "first\n  second\n\nthird" and result.parsed.stack_trace is None


# --- the stack-trace split --------------------------------------------------------------------------------------------

SPRING = "2026-10-29T08:15:23Z ERROR 1 --- [main] c.e.Demo : failed"


def test_blank_lines_before_the_header_are_not_part_of_the_trace():
    result = text(SPRING + "\n\n\njava.io.IOException: disk full\n\tat c.e.A.b(A.java:1)")

    assert result.parsed.message == "failed"
    assert result.parsed.stack_trace == "java.io.IOException: disk full\n\tat c.e.A.b(A.java:1)"
    assert (result.parsed.exception_type, result.parsed.exception_message) == ("java.io.IOException", "disk full")


def test_caused_by_more_and_suppressed_blocks_stay_in_the_trace():
    trace = (
        "java.lang.IllegalStateException: outer\n\tat c.e.A.b(A.java:1)\n"
        "\tSuppressed: java.io.IOException: close failed\n\t\tat c.e.C.d(C.java:2)\n"
        "Caused by: java.sql.SQLException: inner\n\tat c.e.D.e(D.java:3)\n\t... 42 more"
    )

    result = text(SPRING + "\n" + trace)

    assert result.parsed.stack_trace == trace and result.parsed.exception_message == "outer"
    assert "root cause: java.sql.SQLException: inner" in build_semantic_text(result.parsed).text


def test_frames_without_a_header_start_the_trace_and_give_no_exception():
    result = text(SPRING + "\nmore context\n\tat c.e.A.b(A.java:1)\n\tat c.e.B.c(B.java:2)")

    assert result.parsed.stack_trace == "\tat c.e.A.b(A.java:1)\n\tat c.e.B.c(B.java:2)"
    assert (result.parsed.exception_type, result.parsed.exception_message) == (None, None)
    assert result.parsed.message == "failed\nmore context"


def test_an_exception_inside_the_first_line_is_not_split():
    result = text("2026-10-29T08:15:23Z ERROR 1 --- [main] c.e.Demo : failed: java.lang.IllegalStateException: boom")

    assert result.parsed.message == "failed: java.lang.IllegalStateException: boom"
    assert (result.parsed.stack_trace, result.parsed.exception_type) == (None, None)


def test_the_first_of_two_headers_wins_and_the_rest_stays_in_the_trace():
    result = text(SPRING + "\njava.io.IOException: one\n\tat a.B.c(B.java:1)\njava.lang.Error: two")

    assert result.parsed.exception_type == "java.io.IOException"
    assert result.parsed.stack_trace.endswith("java.lang.Error: two")


def test_a_header_without_a_message():
    result = text(SPRING + "\njava.lang.OutOfMemoryError\n\tat a.B.c(B.java:1)")

    assert (result.parsed.exception_type, result.parsed.exception_message) == ("java.lang.OutOfMemoryError", None)


def test_without_a_layout_the_split_still_works_with_value_provenance():
    result = text("something broke\n\njava.io.IOException: disk full\n\tat c.e.A.b(A.java:1)")

    assert result.parsed.message == "something broke"  # 5.5 behaviour: the first line only
    assert result.parsed.stack_trace.startswith("java.io.IOException: disk full")
    assert result.provenance["stack_trace"] == Provenance("value", 0.6, "text", None)
    assert result.provenance["exception_type"] == Provenance("value", 0.6, "text", None)


def test_a_header_on_the_first_line_without_a_layout_gives_the_exception_and_the_frames():
    result = text("java.lang.IllegalStateException: boom\n\tat a.B.c(B.java:1)")

    assert result.parsed.exception_type == "java.lang.IllegalStateException"
    assert result.parsed.stack_trace == "\tat a.B.c(B.java:1)"


def test_a_plain_multi_line_event_keeps_everything_in_the_message():
    result = text(SPRING + "\n  detail one\n  detail two")

    assert result.parsed.message == "failed\n  detail one\n  detail two"
    assert (result.parsed.stack_trace, result.parsed.exception_type) == (None, None)


# --- no layout --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "We shipped --- [v2] today, says Bob : great",
    "2026-10-29T08:15:23Z INFO see --- [docs] for x : y",
    "2026-10-29T08:15:23Z INFO 1 --- [main] two words : y",
    "2026-10-29T08:15:23Z INFO 1 --- [main] a.B: no space before the colon",
    "2026-10-29T08:15:23Z CRITICAL 1 --- [main] a.B : m",
    "29/10/2026 08:15:23 INFO 1 --- [main] a.B : m",
    "08:15:23.412 [main] INFO a.B - m",
    "2026-13-45T08:15:23Z INFO 1 --- [main] a.B : m",
    "2026-10-29T08:15:23Z [main] ERROR boom",
    "", "   ", "\n\n",
], ids=["prose", "no-pid", "logger-with-space", "no-space-colon", "non-layout-level", "non-iso", "time-only",
        "impossible-date", "logback-without-logger", "empty", "spaces", "newlines"])
def test_lines_that_are_not_a_layout_keep_the_value_recognition(line):
    result = text(line)

    assert result.parsed.logger is None and result.fields == {}
    assert all(p.method != "parser" for p in result.provenance.values())
    assert result.parsed.raw_text is line


def test_level_comes_only_from_evidence_when_no_layout_matches():
    assert text("We shipped --- [v2] today : an error was fixed").parsed.level is None
    assert text("2026-10-29T08:15:23Z CRITICAL 1 --- [main] a.B : m").parsed.level == "FATAL"


# --- hostile input ----------------------------------------------------------------------------------------------------

STAMP = "2026-10-29T08:15:23Z"


@pytest.mark.parametrize("make", [
    lambda: f"{STAMP}  INFO 1 --- [" + "t" * 1_000_000 + "] a.B : m",
    lambda: f"{STAMP}  INFO 1 --- " + "[" * 1_000_000,
    lambda: f"{STAMP}  INFO 1 --- [main] a.B" + " : " * 333_333,
    lambda: f"{STAMP}  INFO" + " " * 1_000_000 + "1 --- [main] a.B : m",
    lambda: f"{STAMP} [" + "a" * 1_000_000,
    lambda: f"{STAMP} [main] INFO " + "a" * 1_000_000 + " - m",
    lambda: f"{STAMP}  INFO 1 --- [main] " + "[" * 1_000_000 + " : m",
    lambda: "x" * 1_000_000,
    lambda: f"{STAMP}  INFO 1 --- [main] a.B : m\n" + "\tat a.B.c(B.java:1)\n" * 60_000,
    lambda: f"{STAMP}  INFO 1 --- [main] a.B : m" + "\n" * 1_000_000 + "java.lang.X: late",
    lambda: f"{STAMP}  INFO 1 --- [main] a.B : m\n" + ("x" * 1000 + "\n") * 1000,
    lambda: f"{STAMP}  INFO 1 --- [main] a.B : m\n" + "FooException" * 90_000,
], ids=["huge-thread", "million-brackets", "million-colons", "space-run", "unclosed-thread", "huge-logger",
        "bracket-logger", "one-line", "many-frames", "late-header", "long-message", "huge-header"])
def test_hostile_megabyte_inputs_are_fast_and_bounded(make):
    value = make()
    started = time.perf_counter()

    result = text(value)

    assert time.perf_counter() - started < MAX_SECONDS
    assert result.parsed.raw_text is value
    assert len(result.parsed.message or "") <= 8192 and len(result.parsed.logger or "") <= 512
    assert len(result.parsed.stack_trace or "") <= MAX_STACK_TRACE_CHARS
    assert len(str(result.fields)) < 1024


def random_layout_text(rng: random.Random) -> str:
    pieces = [
        STAMP, "2026-10-29 08:15:23,412", " ", "  ", "INFO", "ERROR", "WARN", "14325", "---", " --- ", " : ", ":", " - ",
        "[", "]", "[main]", "[myapp,a,b]", "c.e.Demo", "o.a.[/].[x]", "\n", "\n\n", "\tat x.Y.z(Y.java:1)",
        "Caused by: ", "java.lang.IllegalStateException: boom", "... 3 more", "Suppressed: ", "traceId=t1", "\x00",
        "é", "😀", "\t", "Exception", "Error",
    ]
    return "".join(rng.choice(pieces) for _ in range(rng.choice([0, 1, 3, 8, 20, 80])))


def test_fuzzed_layout_fragments_never_raise_and_are_deterministic():
    rng = random.Random("text-layouts")
    started = time.perf_counter()
    for _ in range(2000):
        value = random_layout_text(rng)

        result = text(value)

        assert isinstance(result, ResolvedEvent) and result.parsed.raw_text is value
        assert "_resolver" not in result.provenance
        assert_provenance_complete(result)
        assert result == text(value)
        build_semantic_text(result.parsed)
    assert time.perf_counter() - started < 30
