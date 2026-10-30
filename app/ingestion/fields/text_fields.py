"""Finds fields in a plain-text event by recognising values: a leading timestamp, a severity token, an exception
header line and explicit id pairs. Only evidence counts: prose words such as "error" are never a level.

The Spring/Logback layout (thread, logger) and splitting the stack trace off belong to the format parser
(text_layouts), which runs on top of this."""

import re

from app.ingestion.fields.model import TEXT_SOURCE, Found, Provenance
from app.ingestion.fields.values import EXCEPTION_LINE, ISO_TIMESTAMP, LEVELS, timestamp_from_match

MAX_SCAN_CHARS = 64 * 1024  # regex recognition never looks further into an event
MAX_LEVEL_COLUMN = 80  # a severity token must start the line's layout, not its message
MAX_VALUE_CHARS = 8192

_DELIMITERS = r"\s\[\]|"
# An UPPERCASE token between delimiters (Spring pads short levels: "Z  INFO 1 ---"), or a bracketed one in any case.
_LEVEL_TOKEN = re.compile(
    r"\[(?P<bracketed>(?i:" + "|".join(sorted(LEVELS, key=len, reverse=True)) + r"))\]"
    rf"|(?<![^{_DELIMITERS}])(?P<bare>TRACE|DEBUG|INFO|WARNING|WARN|ERROR|FATAL|CRITICAL|SEVERE)(?![^{_DELIMITERS}:])"
)
# What may stand between the timestamp and the level in a log layout: spaces, digits and date/time punctuation, a pid,
# bracketed or parenthesised fields such as the thread. An ordinary word before the token means it is prose
# ("Connection INFO retrieved"), not a level.
_LAYOUT_PREFIX = re.compile(r"(?:[\s\d/:.,+\-TZ]|\[[^\]]*\]|\([^)]*\)|[\w.-]+[ \t]*\|)*")  # "main |" columns too
_ID_PAIR = re.compile(
    r"(?<![\w-])(?:(?P<trace>(?:x-)?trace[_-]?id)|(?P<request>(?:x-)?req(?:uest)?[_-]?id|correlation[_-]?id))"
    r"[ \t]*[=:][ \t]*[\"']?(?P<value>[\w.:-]{1,128})",
    re.IGNORECASE,
)


def resolve_text(text: str, tz_name: str) -> Found:
    head = text[:MAX_SCAN_CHARS]
    first_line = head.split("\n", 1)[0][:MAX_VALUE_CHARS]
    found: Found = {}
    message_start = 0
    if (stamp := ISO_TIMESTAMP.match(first_line)) and (parsed := timestamp_from_match(stamp, tz_name)):
        found["occurred_at"] = (parsed[0], Provenance.of("value", TEXT_SOURCE, parsed[1]))
        message_start = stamp.end()
    message = first_line[message_start:]
    level = _LEVEL_TOKEN.search(first_line, message_start, MAX_LEVEL_COLUMN)
    if level and _LAYOUT_PREFIX.fullmatch(first_line[message_start:level.start()]):
        found["level"] = (LEVELS[(level["bracketed"] or level["bare"]).upper()], Provenance.of("value", TEXT_SOURCE))
        before, after = first_line[message_start:level.start()], first_line[level.end():]
        message = f"{before.rstrip()} {after.lstrip()}"
    if message.strip():
        found["message"] = (message.strip(), Provenance.of("value", TEXT_SOURCE))
    _exception(head, found)
    _ids(head, found)
    return found


def _exception(head: str, found: Found) -> None:
    if (match := EXCEPTION_LINE.search(head)) is None:
        return
    found["exception_type"] = (match[1], Provenance.of("value", TEXT_SOURCE))
    if match[2] and match[2].strip():
        found["exception_message"] = (match[2].strip()[:MAX_VALUE_CHARS], Provenance.of("value", TEXT_SOURCE))


def _ids(head: str, found: Found) -> None:
    for pair in _ID_PAIR.finditer(head):
        name = "trace_id" if pair["trace"] else "request_id"
        if name not in found:  # an explicit key is name evidence, even inside free text
            found[name] = (pair["value"], Provenance.of("name", TEXT_SOURCE))
        if "trace_id" in found and "request_id" in found:
            return
