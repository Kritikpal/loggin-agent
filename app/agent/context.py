"""What the model sees of earlier tool results (ADR-003 layer 3).

Every model call re-sends the whole conversation, so a search result read three turns ago is paid for again on each
later call. Before each call, search results from earlier turns are replaced with a digest: per hit the citation
fields (source, time, level, lines), the first line and a stack trace's last "Caused by". The latest turn's results,
which the model has not read yet, are sent in full. Only the copy sent to the model changes; the agent's state, the API response and the
coverage note keep the full results.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, ToolMessage

FIRST_LINE_MAX = 300  # a log line's message, kept whole up to here; most lines are 100-250 characters
LAST_LINE_MAX = 150  # the last "Caused by:" line of a stack trace
DIGEST_NOTE = "earlier result, shortened to save tokens; its lines can still be cited, search again for full text"
OMITTED = "\n[...]\n"  # where shorten_event dropped lines
_NOT_MESSAGE = re.compile(r"(at |\.\.\. \d+ more|Caused by:|Suppressed:)")  # stack frames end a message


@dataclass(frozen=True)
class DigestSeenResults:
    """A ContextEdit for LangChain's ContextEditingMiddleware."""

    tools: tuple[str, ...] = ("search_logs",)  # summaries are already small and their counts are the answer

    def apply(self, messages: list[AnyMessage], *, count_tokens: Callable[[Sequence[BaseMessage]], int]) -> None:
        latest_turn = max(
            (i for i, m in enumerate(messages) if isinstance(m, AIMessage) and m.tool_calls), default=-1)
        for i in range(latest_turn):
            message = messages[i]
            if isinstance(message, ToolMessage) and message.name in self.tools:
                digest = _digest(message.content if isinstance(message.content, str) else "")
                if digest is not None:
                    messages[i] = message.model_copy(update={"content": digest})


def _digest(content: str) -> str | None:
    """None when there is nothing to shorten: an error, a non-JSON result, or one that is already a digest."""
    try:
        data = json.loads(content)
    except ValueError:
        return None
    if not isinstance(data, dict) or "results" not in data or "digest" in data:
        return None
    # one line per hit: repeating JSON keys on every hit cost more than the shortened text itself
    hits = [f"{h.get('source_id')} L{h.get('line_start')}-{h.get('line_end')} {h.get('level')} {h.get('occurred_at')} | "
            f"{_ends(str(h.get('raw_text', '')))}" for h in data["results"]]
    return json.dumps({"digest": DIGEST_NOTE, "hits": hits, "coverage": data.get("coverage", [])})


def _ends(text: str) -> str:
    return shorten_event(text, first_max=FIRST_LINE_MAX, cause_max=LAST_LINE_MAX)


def shorten_event(text: str, *, first_max: int, cause_max: int) -> str:
    """An event cut by lines, not characters: its message (the lines up to the first stack frame, at most `first_max`
    characters) and a stack trace's last "Caused by:" line (the root cause, at most `cause_max`). Frames are dropped
    and the gap is marked "[...]". A short event comes back unchanged.

    Used for fresh search hits and for digests. Cutting by characters lost facts from the middle of a line twice in the
    eval: "PID 26512" (t1) and "User 35eea954" at character 334 of a 417-character WSO2 error line (t2, t6)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    head = [lines[0] if len(lines[0]) <= first_max else f"{lines[0][:first_max]} [...]"]
    size = len(head[0])
    for line in lines[1:]:  # a message continued on the next lines, e.g. a WSO2 "#### ALERT ####" banner
        if _NOT_MESSAGE.match(line) or size + 1 + len(line) > first_max:
            break
        head.append(line)
        size += 1 + len(line)
    message = "\n".join(head)
    if len(head) == len(lines):
        return message
    causes = [line for line in lines[len(head):] if line.startswith("Caused by:")]
    return f"{message}{OMITTED}{causes[-1][:cause_max]}" if causes else f"{message}{OMITTED.rstrip()}"
