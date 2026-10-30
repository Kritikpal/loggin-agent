"""Groups physical log lines into logical events, so a stack trace (blank lines and all) is ONE event.

Pure: it takes already-read lines and does no file I/O. Offsets are byte offsets into the original log, kept exact
(CRLF included) so an event's evidence can be re-read from `start_offset` to `end_offset`.
"""

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from app.ingestion.sanitize import sanitize

# A line starting with an ISO-8601 timestamp begins a new event (Spring Boot, Logback, most JSON-less loggers).
DEFAULT_EVENT_START = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
EVERY_LINE = re.compile(r"\s*\S")  # JSON logs: each non-blank line is its own event, indented or not
MAX_MATCH_CHARS = 500  # start patterns only look at the beginning of a line (bounds work on hostile lines)


@dataclass(frozen=True)
class AssemblyLimits:
    """Hard caps so a wrong start pattern can never consume unbounded memory."""

    max_lines: int = 500
    max_chars: int = 256 * 1024  # of the event text, newlines included


@dataclass(frozen=True)
class RawEvent:
    text: str  # sanitized, without leading or trailing line breaks
    start_offset: int  # byte offset of the first line
    end_offset: int  # byte offset just after the last line (the next event starts here)
    start_line: int  # 1-based line numbers, inclusive
    end_line: int
    # The caps cut this event. A line longer than max_chars is clipped for good (the rest of that line is not emitted;
    # the offsets still cover it, so it can be re-read); when the line or character cap merely splits an event, the
    # rest follows as the next event.
    truncated: bool


def start_pattern_for(fmt: str, event_start: re.Pattern[str] | None) -> re.Pattern[str]:
    if event_start is not None:
        return event_start
    return EVERY_LINE if fmt == "json" else DEFAULT_EVENT_START


class _Pending:
    """The event being built. Mutable on purpose and private to this module; RawEvent is the immutable result."""

    def __init__(self, offset: int, line_no: int) -> None:
        self.lines: list[str] = []
        self.chars = 0  # length of "\n".join(lines)
        self.truncated = False
        self.start_offset, self.start_line = offset, line_no
        self.end_offset, self.end_line = offset, line_no - 1

    @property
    def blank_only(self) -> bool:
        return not any(line.strip() for line in self.lines)

    def add(self, text: str, end_offset: int, line_no: int, max_chars: int) -> None:
        separator = 1 if self.lines else 0
        room = max(max_chars - self.chars - separator, 0)
        if len(text) > room:
            text, self.truncated = text[:room], True
        self.lines.append(text)
        self.chars += separator + len(text)
        self.end_offset, self.end_line = end_offset, line_no

    def build(self) -> RawEvent:
        text = sanitize("\n".join(self.lines)).strip("\n")
        return RawEvent(text, self.start_offset, self.end_offset, self.start_line, self.end_line, self.truncated)


def assemble(
    lines: Iterable[tuple[int, bytes]],
    start_pattern: re.Pattern[str],
    limits: AssemblyLimits = AssemblyLimits(),
    first_line: int = 1,
) -> Iterator[RawEvent]:
    """`lines` are (byte_offset, raw_line_including_its_terminator) in file order, `first_line` the number of the first.

    Lines before the first start line become their own event (never dropped). Blank lines stay with the event they
    follow, or with the first event when the file starts with them; an event with no text at all is not emitted.
    Undecodable bytes become U+FFFD."""
    pending: _Pending | None = None
    for line_no, (offset, raw) in enumerate(lines, first_line):
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        starts = start_pattern.match(text[:MAX_MATCH_CHARS]) is not None
        too_big = pending is not None and (len(pending.lines) >= limits.max_lines or pending.chars >= limits.max_chars)
        if pending is not None and (starts or too_big) and not (starts and not too_big and pending.blank_only):
            if too_big and not starts:
                pending.truncated = True
            event = pending.build()
            if event.text.strip():
                yield event
            pending = None
        if pending is None:
            pending = _Pending(offset, line_no)
        pending.add(text, offset + len(raw), line_no, limits.max_chars)
    if pending is not None and (event := pending.build()).text.strip():
        yield event
