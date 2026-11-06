"""The agent golden set (tests/fixtures/agent-golden.yaml) is well formed and its cited lines are real event starts.
It does not run the agent; that is the eval's job. This only keeps the expected answers from drifting into typos."""

import re
from collections import Counter

import pytest
import yaml

from scripts.eval_scoring import BUDGETS
from tests.project_helpers import FIXTURE_LOGS
from tests.search_helpers import FIXTURES

GOLDEN = FIXTURES / "agent-golden.yaml"
CLASSES = {"summary_only", "summary_then_search", "search_only", "no_tool", "beyond_tools"}
KINDS = {"common", "edge", "adversarial"}
TOOLS = set(BUDGETS)  # overview, find, search_logs: the registry, copied into the scorer
FIELDS = {"id", "class", "kind", "question", "tools", "forbid", "answer_has", "answer_lacks", "cites", "setup", "note",
          "smoke", "max_model_calls", "expect_batch"}
EVENT_START = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|TID: \[)")

DATA = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
CASES = DATA["cases"]


def lines_of(file: str) -> list[str]:
    # errors="replace": two WSO2 logs contain a few bytes that are not UTF-8
    return (FIXTURE_LOGS / f"{file}.log").read_text(encoding="utf-8", errors="replace").splitlines()


def test_ids_are_unique_and_every_class_is_covered():
    ids = Counter(case["id"] for case in CASES)

    assert [i for i, n in ids.items() if n > 1] == []
    assert {case["class"] for case in CASES} == CLASSES
    assert {case["kind"] for case in CASES} == KINDS
    assert len(CASES) == 17


def test_every_tool_is_required_by_some_case():
    audit = yaml.safe_load((FIXTURES / "agent-golden-audit.yaml").read_text(encoding="utf-8"))["cases"]
    required = {tool for case in [*CASES, *audit] for tool in case["tools"]}  # across both golden files

    assert required == TOOLS


def test_the_smoke_set_has_one_case_of_every_class():
    smoke = Counter(case["class"] for case in CASES if case.get("smoke"))

    assert smoke == dict.fromkeys(CLASSES, 1)


def test_the_runner_selects_only_smoke_cases():
    from scripts.eval_agent import select

    chosen = select(CASES, None, None, smoke=True)

    assert len(chosen) == 5 and all(case["smoke"] for case in chosen)
    assert [c["id"] for c in select(CASES, None, "no_tool", smoke=True)] == ["w10-restart-request"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_each_case_is_well_formed(case):
    assert set(case) <= FIELDS
    assert case["class"] in CLASSES and case["kind"] in KINDS and case["question"].strip()
    tools, forbid = case["tools"], case.get("forbid", [])
    assert set(tools) <= TOOLS and set(forbid) <= TOOLS and not set(tools) & set(forbid)
    assert all(isinstance(fact, list) and fact for fact in case.get("answer_has", []))
    assert case.get("setup") is None or case["setup"] in DATA["setups"]


@pytest.mark.parametrize("case", [c for c in CASES if "cites" in c], ids=lambda c: c["id"])
def test_cited_lines_start_an_event_in_the_fixture(case):
    for source_id, lines in case["cites"].items():
        text = lines_of(source_id)
        bad = [n for n in lines if not (1 <= n <= len(text) and EVENT_START.match(text[n - 1]))]
        assert bad == [], f"{source_id}: lines {bad} are not event starts"
