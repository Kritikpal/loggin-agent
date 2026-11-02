"""Rule-based scoring of one agent run against one golden case (tests/fixtures/agent-golden.yaml). No model calls.

Each check is present only when the case asks for it, except `budget`, which every run must pass:
    tools_order      every tool in `tools` was called, first calls in that order by turn (an empty list requires nothing)
    model_calls      at most `max_model_calls` model calls: turns that asked for tools, plus the final answer
    batched          with `expect_batch`, one turn asked for 2 or more tools (ADR-003 layer 1)
    forbidden_tools  no tool in `forbid` was called, not even a call the budget refused
    budget           the model tried at most 3 searches and 2 summaries (a refused call still counts as trying)
    answer_has       each fact appears, in any of its spellings, case-insensitive
    answer_lacks     none of these texts appears, case-insensitive
    cites            at least one expected line number appears as a whole number ("L158", "line 158")
"""

import re
from collections import Counter
from collections.abc import Mapping, Sequence

BUDGETS = {"search_logs": 3, "summarize_logs": 2}
# Models write "dev‑auth‑service" with non-breaking hyphens and "15:44 IST" with narrow spaces; compare plain text.
_PLAIN = str.maketrans({**dict.fromkeys(map(ord, "‐‑‒–—−"), "-"),
                        **dict.fromkeys(map(ord, "   "), " ")})


def score(case: Mapping, answer: str, tool_calls: Sequence[Mapping]) -> dict[str, bool]:
    names = [call["name"] for call in tool_calls]
    # the model turn that asked for each call; results saved before turns were recorded count one call per turn
    turns = [call.get("turn", n) for n, call in enumerate(tool_calls, 1)]
    answer = answer.translate(_PLAIN)
    text = answer.casefold()
    checks = {"tools_order": _in_order(case.get("tools", []), names, turns)}
    if case.get("max_model_calls"):
        checks["model_calls"] = len(set(turns)) + 1 <= case["max_model_calls"]  # + 1: the final answer
    if case.get("expect_batch"):
        checks["batched"] = max(Counter(turns).values(), default=0) >= 2
    if case.get("forbid"):
        checks["forbidden_tools"] = not set(case["forbid"]) & set(names)
    tried = Counter(names)
    checks["budget"] = all(tried[name] <= limit for name, limit in BUDGETS.items())
    if case.get("answer_has"):
        checks["answer_has"] = all(any(_contains(s, answer, text) for s in fact) for fact in case["answer_has"])
    if case.get("answer_lacks"):
        checks["answer_lacks"] = not any(s.casefold() in text for s in case["answer_lacks"])
    if case.get("cites"):
        lines = {n for numbers in case["cites"].values() for n in numbers}
        checks["cites"] = any(_cited(n, answer) for n in lines)
    return {**checks, "passed": all(checks.values())}


def _in_order(required: Sequence[str], names: Sequence[str], turns: Sequence[int]) -> bool:
    """Compared by turn, so tools asked for together in one turn are in order whichever comes back first."""
    if not set(required) <= set(names):
        return False
    firsts = [turns[names.index(name)] for name in required]
    return firsts == sorted(firsts)


def _contains(expected: str, answer: str, folded: str) -> bool:
    """A bare number must stand alone ("18 errors", not "line 1820" or "10:18:04"); other text is a substring."""
    if str(expected).isdigit():
        return _cited(int(expected), answer)
    return str(expected).casefold() in folded


def _cited(line: int, answer: str) -> bool:
    # not after a digit, '.', ':' or ',' so "15:44:08.158" (milliseconds) and "1580" do not count
    return re.search(rf"(?<![\d.:,]){line}(?!\d)", answer) is not None
