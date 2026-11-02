"""The format parser for plain-text events: a small, explicit set of first-line layouts and the stack-trace split.

A layout applies only when it matches the whole (capped) first line; otherwise the event keeps the value recognition of
text_fields untouched. Either way, the first line after the first one that is an exception header or a Java frame
starts the stack trace. Pure, deterministic and bounded: every pattern is linear-time and sees at most MAX_SCAN_CHARS.
"""

import re
from dataclasses import dataclass

from app.ingestion.fields.model import TEXT_SOURCE, Found, Method, Provenance
from app.ingestion.fields.text_fields import MAX_SCAN_CHARS, MAX_VALUE_CHARS, resolve_text
from app.ingestion.fields.values import EXCEPTION_LINE, ISO_TIMESTAMP, LEVELS, timestamp_from_match
from app.ingestion.sanitize import sanitize

MAX_FIRST_LINE_CHARS = 8192  # a layout is matched against at most this much of the first line
MAX_STACK_TRACE_CHARS = MAX_SCAN_CHARS  # a long trace keeps its last "Caused by", which is the root cause

_TS = f"(?:{ISO_TIMESTAMP.pattern})"
_LEVEL = r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)"  # what %level / %5p print; any other word is no layout
_THREAD = r"\[(?P<thread>[^\]\n]{1,256})\]"
_LOGGER = r"(?P<logger>\S{1,512})"  # one token; may contain brackets: o.a.c.c.C.[.[.[/].[dispatcherServlet]
_MESSAGE = r"(?:[ \t](?P<message>.*))?"  # everything after the first separator that follows the logger
# An optional "[myapp,traceId,spanId]" (Sleuth) or "[myapp]" (Boot 3.2+) group: matched, not interpreted.
_GROUP = r"(?:\[[^\]\n]{0,256}\][ \t]+)?"

# <ts> <LEVEL> <pid> --- [<thread>] <logger> : <message>  (Spring Boot 2/3 default console/file pattern)
SPRING_BOOT = re.compile(
    rf"{_TS}[ \t]+{_LEVEL}[ \t]+{_GROUP}(?P<pid>\d{{1,10}})[ \t]+---[ \t]+{_GROUP}{_THREAD}[ \t]+{_LOGGER}[ \t]+:{_MESSAGE}"
)
# <ts> [<thread>] <LEVEL> <logger> - <message>
LOGBACK_DEFAULT = re.compile(rf"{_TS}[ \t]+{_THREAD}[ \t]+{_LEVEL}[ \t]+{_LOGGER}[ \t]+-{_MESSAGE}")
# TID: [<tenant>] [<app>] [<ts>] [<correlation id>] <LEVEL> {<logger>} - <message>  (WSO2 Carbon, log4j2). The groups are
# often empty, the level is space-padded, some lines (shutdown messages) have no correlation group, and the first lines
# of a start (ConfigParser) have only the time group: "TID: [2026-03-02 16:00:24,804]  INFO {...} - ...".
WSO2_CARBON = re.compile(
    rf"TID:[ \t]+(?:\[(?P<tenant>[^\]\n]{{0,128}})\][ \t]+\[(?P<app>[^\]\n]{{0,128}})\][ \t]+)?\[{_TS}\]"
    rf"(?:[ \t]+\[(?P<correlation>[^\]\n]{{0,128}})\])?[ \t]+{_LEVEL}[ \t]+\{{(?P<logger>[^}}\n]{{1,512}})\}}[ \t]+-{_MESSAGE}"
)
LAYOUTS = (  # tried in order; the first match wins
    ("spring_boot", SPRING_BOOT), ("logback_default", LOGBACK_DEFAULT), ("wso2_carbon", WSO2_CARBON),
)
LAYOUT_FIELDS = frozenset({"occurred_at", "level", "logger", "message"})

# A Java frame line: "\tat pkg.Class.method(" - a dotted name directly followed by "(", so prose "at least" is no frame.
_FRAME_LINE = re.compile(r"[ \t]*at[ \t]+(?=[^\s(]{0,1000}\.)[\w$.<>@/-]{1,1000}\(")


@dataclass(frozen=True)
class _Layout:
    name: str
    found: Found  # occurred_at, level, logger
    message: str  # the first line's message part, possibly empty
    fields: dict[str, object]  # pid, thread


@dataclass(frozen=True)
class _Split:
    start: int  # index of the line where the stack trace starts
    header: re.Match[str] | None  # the exception header line, or None when the trace starts with a frame


def resolve_text_event(text: str, tz_name: str) -> tuple[Found, dict[str, object]]:
    """The canonical fields of a text event and its other fields (pid, thread) as JSON-compatible values."""
    recognised = resolve_text(text, tz_name)
    lines = text[:MAX_SCAN_CHARS].split("\n")
    layout = _parse_layout(lines[0][:MAX_FIRST_LINE_CHARS], tz_name)
    split = _split(lines)
    if layout is None:  # value recognition stays as it was (its exception is the first header too); add the trace
        trace = _trace(lines, split, "value", TEXT_SOURCE)
        return recognised | ({"stack_trace": trace["stack_trace"]} if trace else {}), {}
    found = dict(layout.found)
    end = split.start if split else len(lines)
    message = "\n".join([layout.message, *lines[1:end]]).strip()[:MAX_VALUE_CHARS]
    if message:
        found["message"] = (message, Provenance.of("parser", layout.name))
    found.update(_trace(lines, split, "parser", layout.name))
    for name, value in recognised.items():  # e.g. trace/request id pairs: they fill gaps only
        if name not in LAYOUT_FIELDS:
            found.setdefault(name, value)
    return found, layout.fields


def _parse_layout(line: str, tz_name: str) -> _Layout | None:
    for name, pattern in LAYOUTS:
        match = pattern.fullmatch(line)
        if match is None:
            continue
        stamp = timestamp_from_match(match, tz_name)
        if stamp is None:  # the shape fits but the date is impossible: not this layout
            return None
        provenance = Provenance.of("parser", name)
        found: Found = {
            "occurred_at": (stamp[0], Provenance.of("parser", name, stamp[1])),
            "level": (LEVELS[match["level"]], provenance),
            "logger": (match["logger"], provenance),
        }
        if correlation := sanitize(match.groupdict().get("correlation") or "").strip():
            found["request_id"] = (correlation, provenance)  # WSO2's correlation id ties one request's lines together
        return _Layout(name, found, match["message"] or "", _layout_fields(match))
    return None


def _layout_fields(match: re.Match[str]) -> dict[str, object]:
    groups = match.groupdict()
    fields: dict[str, object] = {}
    if groups.get("pid"):
        fields["pid"] = int(groups["pid"])
    for key in ("thread", "tenant", "app"):  # "[   main]" is padded; empty groups add nothing
        if value := sanitize(groups.get(key) or "").strip():
            fields[key] = value
    return fields


def _split(lines: list[str]) -> _Split | None:
    """Only line-anchored evidence after the first line counts; blank lines before the header are skipped."""
    for index in range(1, len(lines)):
        if header := EXCEPTION_LINE.match(lines[index]):
            return _Split(index, header)
        if _FRAME_LINE.match(lines[index]):
            return _Split(index, None)
    return None


def _trace(lines: list[str], split: _Split | None, method: Method, source: str) -> Found:
    if split is None:
        return {}
    provenance = Provenance.of(method, source)
    found: Found = {"stack_trace": ("\n".join(lines[split.start:]).rstrip()[:MAX_STACK_TRACE_CHARS], provenance)}
    if split.header:
        found["exception_type"] = (split.header[1], provenance)
        if split.header[2] and split.header[2].strip():
            found["exception_message"] = (split.header[2].strip()[:MAX_VALUE_CHARS], provenance)
    return found
