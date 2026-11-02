"""The agent golden set (tests/fixtures/agent-golden.yaml) is well formed and its cited lines are real event starts.
It does not run the agent; that is the eval's job. This only keeps the expected answers from drifting into typos."""

import re
from collections import Counter

import pytest
import yaml

from tests.search_helpers import FIXTURES

GOLDEN = FIXTURES / "agent-golden.yaml"
CLASSES = {"summary_only", "summary_then_search", "search_only", "no_tool", "beyond_tools"}
KINDS = {"common", "edge", "adversarial"}
TOOLS = {"search_logs", "summarize_logs"}
FIELDS = {"id", "class", "kind", "question", "tools", "forbid", "answer_has", "answer_lacks", "cites", "setup", "note",
          "smoke"}
SOURCE_FILES = {
    "wso2-carbon-0107": "wso2carbon-01-07-2026.1.log",
    "wso2-carbon-0114": "wso2carbon-01-14-2026.1.log",
    "wso2-carbon-0127": "wso2carbon-01-27-2026.log",
    "wso2-carbon-0302": "wso2carbon-03-02-2026.1.log",
    "wso2-carbon-1028": "wso2carbon-10-28-2026.1.log",
    "wso2-carbon-1030": "wso2carbon-10-30-2026.1.log",
    "wso2-carbon-1101": "wso2carbon-11-01-2026.1.log",
}
EVENT_START = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|TID: \[)")

DATA = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
CASES = DATA["cases"]


def lines_of(source_id: str) -> list[str]:
    # errors="replace": two WSO2 logs contain a few bytes that are not UTF-8
    return (FIXTURES / SOURCE_FILES[source_id]).read_text(encoding="utf-8", errors="replace").splitlines()


def test_ids_are_unique_and_every_class_is_covered():
    ids = Counter(case["id"] for case in CASES)

    assert [i for i, n in ids.items() if n > 1] == []
    assert {case["class"] for case in CASES} == CLASSES
    assert len(CASES) >= 10


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
