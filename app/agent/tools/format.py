"""The compact text the agent's tools return, and its digests (ADR-003 layers 2-3).

Results are text rows under a `columns:` header, not JSON: repeating keys on every hit cost more tokens than the
values. Every result ends with one coverage line (`files 7, indexed 7; stale: a, b`), which the code-side coverage note
(app.agent.graph) reads back with `parse_coverage`. A digest keeps a row's citation fields (time, line, sig) and cuts
only its text; the marker on its first line keeps it from being digested twice.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, ToolMessage

from app.ingestion.fields.resolver import resolve
from app.ingestion.sources import SourceBase
from app.schemas.search_logs import SourceCoverage

MAX_COVERAGE_NAMES = 10  # file names listed in the coverage line; 30 daily files must not ride on every result
MAX_RESULT_CHARS = 4000  # one tool result, ~1,000 tokens (ADR-003)
MAX_ROW_TEXT = 300  # a find row's text: the first line and the last "Caused by"
MAX_CAUSE = 120
CUT = " [...] "  # where a row's text dropped stack frames
DIGEST_MARK = "(earlier result, shortened; rows can still be cited; fetch again for full text)"
FIND_DIGEST_TEXT = 80  # find rows keep this much text: the sig re-fetches the rest
OVERVIEW_DIGEST_TEMPLATE = 60
# search rows keep lines whole up to FIRST_LINE_MAX: cutting shorter lost "PID 26512" and a user id at 334 (ADR-003)
FIRST_LINE_MAX = 300  # a log line's message, kept whole up to here; most lines are 100-250 characters
LAST_LINE_MAX = 150  # the last "Caused by:" line of a stack trace
OMITTED = "\n[...]\n"  # where shorten_event dropped lines
_NOT_MESSAGE = re.compile(r"(at |\.\.\. \d+ more|Caused by:|Suppressed:)")  # stack frames end a message
_COVERAGE = re.compile(r"^files (\d+), indexed (\d+)(.*)$", re.M)
_REPEAT = re.compile(r" ×\d+ until \S+$")


@dataclass(frozen=True)
class Row:
    """One event (or a run of repeats of one kind) as a find or search_logs row."""

    file: str
    occurred_at: datetime | None
    line: int | None
    level: str | None
    sig: str | None
    text: str
    extra: str = ""  # an extra column before the text (search_logs: which legs matched)
    repeat: tuple[int, datetime] | None = None  # (events, last time) of a folded run


def short(source_id: str, project_id: str) -> str:
    return source_id.removeprefix(f"{project_id}/")


def clock(at: datetime | None, *, with_day: bool) -> str:
    """UTC time; with the day as MM-DDT so a row's columns stay split by single spaces."""
    if at is None:
        return "-"
    at = at.astimezone(UTC)
    return f"{at:%m-%dT%H:%M:%SZ}" if with_day else f"{at:%H:%M:%SZ}"


def event_rows(rows: Sequence[Row], columns: str, *, total_label: str, total: int) -> tuple[str, list[str]]:
    """The header and the row lines. The file moves to the header when all rows share one, the day when all share one."""
    files = {r.file for r in rows}
    days = {r.occurred_at.astimezone(UTC).date() for r in rows if r.occurred_at}
    one_day = len(days) <= 1
    head = []
    if len(files) == 1:
        head.append(f"file {next(iter(files))}")
    if one_day and days:
        head.append(f"day {next(iter(days)):%Y-%m-%d}")
    head += [f"{total_label} {total}", f"shown {len(rows)}"]
    cols = ("file " if len(files) > 1 else "") + columns
    lines = [_row_line(r, with_file=len(files) > 1, with_day=not one_day) for r in rows]
    return " · ".join(head) + f" · columns: {cols}", lines


def _row_line(row: Row, *, with_file: bool, with_day: bool) -> str:
    parts = [row.file] if with_file else []
    parts += [clock(row.occurred_at, with_day=with_day), f"L{row.line}" if row.line else "L?",
              (row.level or "-")[:16], row.sig or "-"]  # the level is parsed from untrusted text: keep it a label
    if row.extra:
        parts.append(row.extra)
    text = row.text
    if row.repeat:
        text += f" ×{row.repeat[0]} until {clock(row.repeat[1], with_day=with_day)}"
    return " ".join([*parts, text])


def row_text(raw: str, source: SourceBase | None = None, *, needle: str | None = None, limit: int = MAX_ROW_TEXT,
             cause: int = MAX_CAUSE) -> str:
    """An event on one line: its message (from where the parser found it, since time, level and logger have their own
    columns) and a stack trace's last "Caused by:" (shorten_event), at most `limit` characters. With `needle` (find by
    text), a match that would be cut off is shown as a snippet centred on it: a WSO2 user id sits at character 334 of
    a 417-character line, and "error code 49" can sit deep in a stack trace."""
    raw = _from_message(raw, source)
    short_ = shorten_event(raw, first_max=limit, cause_max=cause)
    head, sep, tail = short_.partition(OMITTED)
    if not sep and short_.endswith(OMITTED.rstrip()):  # frames dropped, no "Caused by" to show
        head, sep = short_.removesuffix(OMITTED.rstrip()), OMITTED
    head = " ".join(head.split()).removesuffix(" [...]")  # shorten_event marks a cut first line with " [...]"
    tail = " ".join(tail.split())
    room = limit - (len(tail) + len(CUT) if sep else 0)
    head = head if len(head) <= room else head[:room - 1] + "…"
    shown = f"{head}{CUT}{tail}".rstrip() if sep else head
    if needle and needle.casefold() not in shown.casefold():
        snippet = _around(" ".join(raw.split()), needle, limit // 2)
        if snippet:
            return f"{head[:limit // 2 - 3]}… {snippet}"
    return shown


def _from_message(raw: str, source: SourceBase | None) -> str:
    """The event from its message on: "TID: [] [] [2026-10-30 18:41:00,969] [] INFO {org.wso2...} - " repeats what
    the row's columns already say, on every row."""
    if source is None:
        return raw
    try:
        message = resolve(raw, source).parsed.message
    except Exception:  # resolve never raises by contract; the raw text is still a fine row
        return raw
    at = raw.find(message.splitlines()[0][:60]) if message and message.strip() else -1
    return raw[at:] if at > 0 else raw


def _around(text: str, needle: str, width: int) -> str | None:
    at = text.casefold().find(needle.casefold())
    if at < 0:
        return None
    start = max(0, at + len(needle) // 2 - width // 2)
    end = min(len(text), start + width)
    return f"{'…' if start else ''}{text[start:end]}{'…' if end < len(text) else ''}"


def fit(header: str, lines: Sequence[str], footer: Sequence[str], *, what: str,
        priority: Sequence[int] | None = None) -> tuple[list[str], str | None]:
    """Drops rows until the result fits MAX_RESULT_CHARS: the lowest `priority` first (latest first among equals),
    or simply the last rows without one. The rest keep their order; the note says how many were left out."""
    budget = MAX_RESULT_CHARS - len(header) - sum(len(f) + 1 for f in footer) - 80  # 80: room for the note
    size = sum(len(line) + 1 for line in lines)
    ranks = priority or [0] * len(lines)
    dropped: set[int] = set()
    for i in sorted(range(len(lines)), key=lambda i: (ranks[i], -i)):
        if size <= budget:
            break
        dropped.add(i)
        size -= len(lines[i]) + 1
    kept = [line for i, line in enumerate(lines) if i not in dropped]
    left_out = len(dropped)
    return kept, (f"{left_out} more {what} left out to keep the result short; narrow the request" if left_out else None)


def coverage_line(coverage: Sequence[SourceCoverage], project_id: str) -> str:
    """One line for the project (spec §6.3): counts, then only the files that are not complete, grouped by status."""
    indexed = sum(c.status != "not_indexed" for c in coverage)
    named: dict[str, list[str]] = {}
    more = 0
    for item in coverage:
        if item.status == "complete":
            continue
        if sum(map(len, named.values())) < MAX_COVERAGE_NAMES:
            named.setdefault(item.status, []).append(short(item.source_id, project_id))
        else:
            more += 1
    parts = [f"files {len(coverage)}, indexed {indexed}", *(f"{s}: {', '.join(n)}" for s, n in named.items())]
    return "; ".join(parts + ([f"more {more}"] if more else []))


def parse_coverage(result: str) -> dict:
    """The coverage line back as {"files": n, "indexed": k, "<status>": [file, ...], "more": m}; {} when absent."""
    found = _COVERAGE.findall(result or "")
    if not found:
        return {}
    files, indexed, rest = found[-1]
    coverage: dict = {"files": int(files), "indexed": int(indexed)}
    for part in filter(None, (p.strip() for p in rest.split(";"))):
        if part.startswith("more ") and part[5:].isdigit():
            coverage["more"] = int(part[5:])
            continue
        status, _, names = part.partition(":")
        coverage[status.strip()] = [n.strip() for n in names.split(",") if n.strip()]
    return coverage


def assemble(header: str, lines: Sequence[str], note: str | None, coverage: str) -> str:
    return "\n".join([header, *lines, *([f"note: {note}"] if note else []), coverage])


# --- digests: what the model sees of a result it has already read -------------------------------------------------


def digest_find(result: str) -> str | None:
    return _digest_rows(result, lambda text: _cut_keep_repeat(text, FIND_DIGEST_TEXT))


def digest_overview(result: str) -> str | None:
    return _digest_rows(result, lambda text: _cut_keep_repeat(text, OVERVIEW_DIGEST_TEMPLATE))


def digest_search(result: str) -> str | None:
    def cut(text: str) -> str:
        head, sep, tail = text.partition(CUT.strip())
        head = head.strip()
        head = head if len(head) <= FIRST_LINE_MAX else head[:FIRST_LINE_MAX] + "…"
        return f"{head}{CUT}{tail.strip()[:LAST_LINE_MAX]}" if sep else head
    return _digest_rows(result, cut)


COUNT_DIGEST_ROWS = 10


def digest_count(result: str) -> str | None:
    """A count_by result seen again: the header, the largest rows, the notes and the coverage line. None when there is
    nothing to shorten: an error, a short result, or one that is already a digest."""
    lines = (result or "").splitlines()
    if len(lines) <= COUNT_DIGEST_ROWS + 2 or lines[0].startswith((DIGEST_MARK, "error:")) or "columns: " not in lines[0]:
        return None
    body = lines[1:-1]
    rows = [line for line in body if not line.startswith("note:")]
    notes = [line for line in body if line.startswith("note:")]
    digest = "\n".join([DIGEST_MARK, lines[0], *rows[:COUNT_DIGEST_ROWS], *notes, lines[-1]])
    return digest if len(digest) < len(result) else None


def _digest_rows(result: str, cut) -> str | None:
    """None when there is nothing to shorten: an error, an unknown format, or one that is already a digest."""
    lines = (result or "").splitlines()
    if not lines or lines[0].startswith((DIGEST_MARK, "error:")) or "columns: " not in lines[0]:
        return None
    columns = lines[0].split("columns: ", 1)[1].split()
    before_text = columns.index("text") if "text" in columns else columns.index("template")
    out = [DIGEST_MARK, lines[0]]
    for line in lines[1:]:
        fields = line.split(" ", before_text)
        is_row = len(fields) > before_text and not line.startswith((" ", "note:", "files "))
        out.append(" ".join([*fields[:before_text], cut(fields[before_text])]) if is_row else line)
    digest = "\n".join(out)
    return digest if len(digest) < len(result) else None  # short rows: the mark would only add tokens


def _cut_keep_repeat(text: str, size: int) -> str:
    repeat = _REPEAT.search(text)
    body = text[:repeat.start()] if repeat else text
    body = body if len(body) <= size else body[:size] + "…"
    return body + (repeat.group(0) if repeat else "")


Digest = Callable[[str], str | None]


@dataclass(frozen=True)
class DigestSeenResults:
    """A ContextEdit for LangChain's ContextEditingMiddleware. `digests`: tool name -> its digest (app.agent.tools);
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
