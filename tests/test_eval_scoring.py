"""The golden-set scorer: tool order, forbidden tools, budgets, facts and citations. No model, no database."""

from scripts.eval_scoring import score


def call(name: str, status: str = "success") -> dict:
    return {"name": name, "status": status, "args": {}}


CASE = {
    "tools": ["summarize_logs", "search_logs"], "forbid": [], "answer_has": [["3", "three"], ["SPRING_DATASOURCE_URL"]],
    "answer_lacks": ["no errors"], "cites": {"dev-auth-service": [158, 167]},
}
GOOD = "There are three errors; the cause is SPRING_DATASOURCE_URL (dev-auth-service L167, 15:44 IST)."


def test_a_good_run_passes_every_check():
    result = score(CASE, GOOD, [call("summarize_logs"), call("search_logs"), call("search_logs")])

    assert result == {"tools_order": True, "budget": True, "answer_has": True, "answer_lacks": True, "cites": True,
                      "passed": True}


def test_tools_in_the_wrong_order_or_missing_fail():
    assert not score(CASE, GOOD, [call("search_logs"), call("summarize_logs")])["tools_order"]
    assert not score(CASE, GOOD, [call("search_logs")])["tools_order"]


def test_a_forbidden_tool_fails_even_when_it_was_refused():
    case = {"tools": ["search_logs"], "forbid": ["summarize_logs"]}

    result = score(case, "x", [call("summarize_logs", status="error"), call("search_logs")])

    assert result["forbidden_tools"] is False and result["passed"] is False


def test_no_tool_cases_fail_on_any_call():
    case = {"tools": [], "forbid": ["search_logs", "summarize_logs"]}

    assert score(case, "I can only help with the indexed logs.", [])["passed"]
    assert not score(case, "I can only help with the indexed logs.", [call("search_logs")])["passed"]


def test_trying_to_go_over_a_budget_fails():
    result = score({"tools": ["search_logs"]}, "x", [call("search_logs")] * 3 + [call("search_logs", "error")])

    assert result["budget"] is False


def test_facts_are_case_insensitive_and_any_spelling_counts():
    assert score({"tools": [], "answer_has": [["usernotfound", "user not found"]]}, "UserNotFound x18", [])["passed"]
    assert not score({"tools": [], "answer_has": [["19"]]}, "18 warnings", [])["passed"]


def test_a_number_fact_must_be_a_whole_number_not_part_of_a_line_or_time():
    case = {"tools": [], "answer_has": [["18", "eighteen"]]}

    assert score(case, "There are 18 errors.", [])["passed"]
    assert score(case, "eighteen errors", [])["passed"]
    assert not score(case, "5 entries, see lines 1820-1917", [])["passed"]
    assert not score(case, "at 10:18:04", [])["passed"]


def test_unicode_hyphens_and_spaces_match_their_plain_forms():
    case = {"tools": [], "answer_has": [["dev-auth-service"], ["15:44 IST"]]}

    assert score(case, "dev‑auth‑service failed at 15:44 IST", [])["passed"]


def test_a_line_must_appear_as_a_whole_number_also_after_an_l():
    case = {"tools": [], "cites": {"s": [158]}}

    assert score(case, "see L158", [])["cites"]
    assert score(case, "line 158.", [])["cites"]
    assert not score(case, "see L1580", [])["cites"]
    assert not score(case, "15:44:08.158", [])["cites"]  # a timestamp's milliseconds are not a citation


def test_forbidden_text_fails():
    assert not score({"tools": [], "answer_lacks": ["0 errors"]}, "There are 0 errors.", [])["answer_lacks"]
