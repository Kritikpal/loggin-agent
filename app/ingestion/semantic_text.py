"""Builds the text that is embedded and keyword-indexed for one parsed, already-redacted log event.

Pure and deterministic: no I/O, no LLM. Redaction must have run before anything reaches this module.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# Total size ~450 tokens at ~4 chars/token, inside bge-small's 512-token window.
MAX_TEXT_CHARS = 1800
MAX_MESSAGE_CHARS = 400
MAX_EXCEPTION_CHARS = 300
MAX_ROOT_CAUSE_CHARS = 300
MAX_STACK_FRAMES = 5
MAX_FRAME_CHARS = 150
MAX_EXTRA_FIELDS = 5
MAX_EXTRA_VALUE_CHARS = 100
MAX_RAW_FALLBACK_CHARS = 1500

# Java stack frames from these are skipped so application code shows first. Used only when a source does not name its
# own packages (`app_packages`), which is the precise way. ponytail: Java/Spring traces only; add Python tracebacks
# (File "...", line N) when there is a real sample to test against.
LIBRARY_FRAME_PREFIXES = (
    "java.", "javax.", "jakarta.", "jdk.", "sun.", "com.sun.", "kotlin.", "kotlinx.", "reactor.",
    "org.springframework.", "org.apache.", "org.hibernate.", "org.jboss.", "org.eclipse.jetty.", "org.slf4j.",
    "org.junit.", "io.netty.", "io.micrometer.", "com.zaxxer.", "com.fasterxml.", "ch.qos.logback.",
)


@dataclass(frozen=True)
class ParsedEvent:
    """What the field resolver extracted from one event. Every field except `raw_text` may be missing."""

    raw_text: str
    level: str | None = None
    logger: str | None = None
    message: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None
    stack_trace: str | None = None
    trace_id: str | None = None
    request_id: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)  # JSON values of any type; stringified when used


@dataclass(frozen=True)
class SemanticText:
    text: str
    truncated: bool  # content was lost to a size limit (clipped, or parts dropped for space)
    filtered_frames: int = 0  # library stack frames left out on purpose; this does not count as truncation


# A Java frame: "at [module/]pkg.Class.method(File.java:42)" - a dotted name followed directly by (...), so prose
# that merely starts with "at " ("at least one pool is busy") is not mistaken for a frame.
_FRAME = re.compile(r"^\s*at\s+((?=[^\s(]*\.)[\w$.<>@/-]+\([^()]*\).*)$")
_MODULE_PREFIX = re.compile(r"^[\w.@-]+/+")  # "java.base/java.lang.Thread.run" -> "java.lang.Thread.run"
MAX_TRACE_LINE_CHARS = 2000  # log content is untrusted: bounds the regex work per line
_JAR_SUFFIX = re.compile(r"\s+~?\[[^\]]*\]$")  # " ~[spring-web-6.1.1.jar:6.1.1]" is noise for search
_CAUSED_BY = "Caused by:"


@dataclass(frozen=True)
class _Trace:
    header: str | None  # first line: "ExceptionType: message"
    root_cause: str | None  # last "Caused by:" line
    frames: list[str]  # top application frames of the deepest cause
    more_frames: bool
    filtered_frames: int


def _is_library(frame: str, app_packages: tuple[str, ...]) -> bool:
    if app_packages:  # the source named its own packages: everything else is a library
        return not frame.startswith(app_packages)
    return frame.startswith(LIBRARY_FRAME_PREFIXES)


def _parse_trace(trace: str, app_packages: tuple[str, ...]) -> _Trace:
    header = root_cause = None
    frames: list[str] = []
    suppressed_indent: int | None = None  # lines indented deeper than a "Suppressed:" line belong to that side exception
    for raw in trace.splitlines():
        raw = raw[:MAX_TRACE_LINE_CHARS].rstrip()
        line = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if suppressed_indent is not None:
            if indent > suppressed_indent:
                continue
            suppressed_indent = None
        if line.startswith("Suppressed:"):
            suppressed_indent = indent
        elif match := _FRAME.match(raw):
            frame = match.group(1)
            if frame.endswith("]"):  # only then can there be a jar suffix to strip
                frame = _JAR_SUFFIX.sub("", frame)
            frames.append(_MODULE_PREFIX.sub("", frame.strip()))
        elif line.startswith(_CAUSED_BY):
            root_cause = line.removeprefix(_CAUSED_BY).strip()
            frames = []  # frames after a "Caused by" belong to that cause; the deepest one is the most telling
        elif line and not line.startswith("...") and header is None:
            header = line
    application = [f for f in frames if not _is_library(f, app_packages)]
    candidates = application or frames  # all-library trace: better some frames than none
    filtered = len(frames) - len(application) if application else 0
    return _Trace(header, root_cause, candidates[:MAX_STACK_FRAMES], len(candidates) > MAX_STACK_FRAMES, filtered)


def build_semantic_text(event: ParsedEvent, app_packages: tuple[str, ...] = ()) -> SemanticText:
    """Join the parts that exist, in priority order, within MAX_TEXT_CHARS.

    Core parts (level through root cause) are clipped to fit; optional ones (stack frames, extra fields) are dropped
    whole once the budget runs out, lowest priority first. `app_packages` (for example ("com.acme",)) are the
    source's own Java packages: when given, only their frames count as application code."""
    prefixes = tuple(p if p.endswith(".") else p + "." for p in app_packages)  # "com.acme" must not match "com.acmecorp"
    # (text, per-part cap, core) so one pass can both clip and tell whether anything was cut
    lines: list[tuple[str, int, bool]] = []

    def add(text: str | None, cap: int, core: bool = True) -> None:
        if text and text.strip():
            lines.append((text.strip(), cap, core))

    trace = _parse_trace(event.stack_trace, prefixes) if event.stack_trace else None
    exception = ": ".join(p for p in (event.exception_type, event.exception_message) if p)
    if not exception and trace:
        exception = trace.header or ""

    ids = " ".join(f"{name}={value}" for name, value in (("trace_id", event.trace_id), ("request_id", event.request_id)) if value)
    add(" ".join(p for p in (event.level, event.logger) if p), MAX_MESSAGE_CHARS)
    if (event.message or "").strip() or exception.strip():
        add(event.message, MAX_MESSAGE_CHARS)
        add(ids, MAX_MESSAGE_CHARS)
    else:
        add(ids, MAX_MESSAGE_CHARS)  # before the long raw text, so the IDs are never squeezed out of the budget
        add(event.raw_text, MAX_RAW_FALLBACK_CHARS)  # never drop an event that has nothing parsed
    add(exception, MAX_EXCEPTION_CHARS)
    if trace:
        if trace.root_cause:
            add(f"root cause: {trace.root_cause}", MAX_ROOT_CAUSE_CHARS)
        for frame in trace.frames:
            add(f"at {frame}", MAX_FRAME_CHARS, core=False)
    for key, value in sorted(event.extra.items())[:MAX_EXTRA_FIELDS]:  # sorted: same fields give the same text
        text = "" if value is None else str(value).strip()  # JSON logs carry ints, bools and nulls too
        if text:
            add(f"{key}={text}", len(key) + 1 + MAX_EXTRA_VALUE_CHARS, core=False)

    kept: list[str] = []
    used = 0
    cut = len(event.extra) > MAX_EXTRA_FIELDS or bool(trace and trace.more_frames)
    dropping = False  # once an optional part does not fit, every later optional part is dropped too
    for line, cap, core in lines:
        part = line[:cap]
        cut = cut or len(line) > cap
        room = MAX_TEXT_CHARS - used - (1 if kept else 0)  # 1 = the newline joining it to the previous part
        if len(part) > room:
            cut = True
            if not core or room <= 0:
                dropping = dropping or not core
                continue
            part = part[:room]
        elif dropping and not core:
            continue
        used += len(part) + (1 if kept else 0)
        kept.append(part)
    return SemanticText(text="\n".join(kept), truncated=cut, filtered_frames=trace.filtered_frames if trace else 0)
