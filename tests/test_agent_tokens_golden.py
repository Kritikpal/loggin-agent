"""The ADR-003 token golden set (tests/fixtures/agent-tokens-golden.yaml) is well formed and cites real event starts."""

import pytest
import yaml

from scripts.eval_agent import select
from tests.test_agent_golden import EVENT_START, KINDS, TOOLS, lines_of
from tests.search_helpers import FIXTURES

DATA = yaml.safe_load((FIXTURES / "agent-tokens-golden.yaml").read_text(encoding="utf-8"))
CASES = DATA["cases"]
CLASSES = {"multi_part", "why_chain", "heavy_results", "timeline", "baseline"}
FIELDS = {"id", "class", "kind", "question", "tools", "max_model_calls", "expect_batch", "answer_has", "cites", "note"}


def test_every_class_is_covered_and_multi_part_questions_expect_batching():
    assert {case["class"] for case in CASES} == CLASSES
    assert len({case["id"] for case in CASES}) == len(CASES)
    assert all(case["expect_batch"] for case in CASES if case["class"] == "multi_part")
    assert not any(case["expect_batch"] for case in CASES if case["class"] != "multi_part")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_each_case_is_well_formed_and_scorable(case):
    assert set(case) <= FIELDS and case["kind"] in KINDS and set(case["tools"]) <= TOOLS
    assert case["max_model_calls"] >= 2  # at least one lookup plus the answer
    assert case["answer_has"] and all(isinstance(fact, list) and fact for fact in case["answer_has"])


@pytest.mark.parametrize("case", [c for c in CASES if "cites" in c], ids=lambda c: c["id"])
def test_cited_lines_start_an_event_in_the_fixture(case):
    for source_id, lines in case["cites"].items():
        text = lines_of(source_id)
        bad = [n for n in lines if not (1 <= n <= len(text) and EVENT_START.match(text[n - 1]))]
        assert bad == [], f"{source_id}: lines {bad} are not event starts"


def test_the_runner_selects_cases_from_this_file_by_class():
    assert [c["id"] for c in select(CASES, None, "why_chain")] == ["t5-jwt-filter-cause-chain"]
