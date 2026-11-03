"""What the model sees of earlier tool results (ADR-003 layer 3).

Every model call re-sends the whole conversation, so a result read three turns ago is paid for again on each later
call. Before each call, results from earlier turns are replaced with each tool's digest (app.agent.registry: find rows
keep time, line, sig and some text; search rows keep lines whole up to 300 characters; overview keeps counts and
citations). The latest turn's results, which the model has not read yet, are sent in full. Only the copy sent to the
model changes; the agent's state, the API response and the coverage note keep the full results.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, ToolMessage

FIRST_LINE_MAX = 300  # a log line's message, kept whole up to here; most lines are 100-250 characters
LAST_LINE_MAX = 150  # the last "Caused by:" line of a stack trace
OMITTED = "\n[...]\n"  # where shorten_event dropped lines
_NOT_MESSAGE = re.compile(r"(at |\.\.\. \d+ more|Caused by:|Suppressed:)")  # stack frames end a message

Digest = Callable[[str], str | None]


@dataclass(frozen=True)
class DigestSeenResults:
    """A ContextEdit for LangChain's ContextEditingMiddleware. `digests`: tool name -> its digest (app.agent.registry);
    a tool without one is sent as it is."""

    digests: Mapping[str, Digest] = field(default_factory=dict)

    def apply(self, messages: list[AnyMessage], *, count_tokens: Callable[[Sequence[BaseMessage]], int]) -> None:
        latest_turn = max(
            (i for i, m in enumerate(messages) if isinstance(m, AIMessage) and m.tool_calls), default=-1)
        for i in range(latest_turn):
            message = messages[i]
            digest = self.digests.get(message.name) if isinstance(message, ToolMessage) else None
            if digest is not None:
                shortened = digest(message.content if isinstance(message.content, str) else "")
                if shortened is not None:
                    messages[i] = message.model_copy(update={"content": shortened})


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
