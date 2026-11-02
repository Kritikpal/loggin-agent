"""Older search results are sent to the model as digests (ADR-003 layer 3); the latest turn's results stay full."""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.context import DigestSeenResults, FIRST_LINE_MAX, LAST_LINE_MAX
from tests.search_helpers import DEV_AUTH_LOG

TRACE = "java.lang.RuntimeException: Driver claims to not accept jdbcUrl " + "\tat frame\n" * 200 + "Caused by: ROOT"


def search_result(lines: int, text: str = TRACE) -> str:
    hit = {"source_id": "dev-auth-service", "occurred_at": "2026-03-10T10:14:08+00:00", "level": "ERROR",
           "line_start": lines, "line_end": lines + 5, "raw_text": text, "score": 0.03, "matched_by": ["keyword"]}
    return json.dumps({"results": [hit], "coverage": [{"source_id": "dev-auth-service", "status": "complete"}]})


def turn(call_id: str, name: str = "search_logs") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id, "type": "tool_call"}])


def conversation() -> list:
    return [
        HumanMessage("why?"),
        turn("c1"), ToolMessage(search_result(158), tool_call_id="c1", name="search_logs"),
        turn("s1", "summarize_logs"), ToolMessage('{"groups": [], "coverage": []}', tool_call_id="s1",
                                                  name="summarize_logs"),
        turn("c2"), ToolMessage(search_result(167), tool_call_id="c2", name="search_logs"),
    ]


def edited() -> list:
    messages = conversation()
    DigestSeenResults().apply(messages, count_tokens=lambda m: 0)
    return messages


def test_a_search_result_from_an_earlier_turn_becomes_a_digest_that_keeps_every_citation_field():
    digest = json.loads(edited()[2].content)

    (hit,) = digest["hits"]
    citation, text = hit.split(" | ", 1)
    assert citation == "dev-auth-service L158-163 ERROR 2026-03-10T10:14:08+00:00"
    assert len(text) <= FIRST_LINE_MAX + LAST_LINE_MAX + 10
    assert text.startswith("java.lang.RuntimeException") and text.endswith("Caused by: ROOT")
    assert digest["coverage"] == [{"source_id": "dev-auth-service", "status": "complete"}]
    assert "digest" in digest


def test_the_latest_turns_results_are_sent_in_full_because_the_model_has_not_read_them_yet():
    assert edited()[6].content == search_result(167)


def test_summaries_and_errors_are_left_alone():
    messages = conversation()
    messages[2] = ToolMessage('{"error": "search is temporarily unavailable"}', tool_call_id="c1", name="search_logs")

    DigestSeenResults().apply(messages, count_tokens=lambda m: 0)

    assert messages[2].content == '{"error": "search is temporarily unavailable"}'
    assert messages[4].content == '{"groups": [], "coverage": []}'


def test_a_short_event_is_kept_whole():
    messages = conversation()
    messages[2] = ToolMessage(search_result(2, "PID 26512"), tool_call_id="c1", name="search_logs")

    DigestSeenResults().apply(messages, count_tokens=lambda m: 0)

    assert json.loads(messages[2].content)["hits"][0].endswith("| PID 26512")


def test_digesting_twice_changes_nothing():
    messages = edited()
    once = [m.content for m in messages]

    DigestSeenResults().apply(messages, count_tokens=lambda m: 0)

    assert [m.content for m in messages] == once


def digest_of(text: str) -> str:
    messages = conversation()
    messages[2] = ToolMessage(search_result(2, text), tool_call_id="c1", name="search_logs")
    DigestSeenResults().apply(messages, count_tokens=lambda m: 0)
    return json.loads(messages[2].content)["hits"][0].split(" | ", 1)[1]


def test_a_long_single_line_keeps_the_fact_in_its_middle():
    # eval t1: the head-and-tail digest cut "PID 26512" out of this line, and the agent then said no PID was logged
    pid_line = DEV_AUTH_LOG.read_text(encoding="utf-8").splitlines()[1]

    assert "PID 26512" in digest_of(pid_line)


def test_a_single_line_over_the_limit_keeps_its_start():
    assert digest_of("x" * 1000) == "x" * FIRST_LINE_MAX + " [...]"


def test_a_stack_trace_keeps_its_first_line_and_its_last_caused_by_not_the_frames():
    trace = ("org.example.BootException: Error creating bean jwtAuthFilter\n\tat a.b(C.java:1)\n"
             "Caused by: java.lang.RuntimeException: Driver claims to not accept jdbcUrl, ${SPRING_DATASOURCE_URL}\n"
             "\tat d.e(F.java:2)\n\t... 40 more")

    text = digest_of(trace)

    assert text.startswith("org.example.BootException: Error creating bean jwtAuthFilter")
    assert text.endswith("Caused by: java.lang.RuntimeException: Driver claims to not accept jdbcUrl, ${SPRING_DATASOURCE_URL}")
    assert "at a.b" not in text and "40 more" not in text
