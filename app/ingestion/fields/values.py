"""Recognises levels, timestamps and exception headers from their values. Pure; every pattern is linear-time."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Every spelling we accept, normalised to TRACE, DEBUG, INFO, WARN, ERROR or FATAL. Anything else is no level.
LEVELS = {
    "TRACE": "TRACE", "FINER": "TRACE", "FINEST": "TRACE",
    "DEBUG": "DEBUG", "FINE": "DEBUG",
    "INFO": "INFO", "NOTICE": "INFO",
    "WARN": "WARN", "WARNING": "WARN",
    "ERROR": "ERROR", "ERR": "ERROR", "SEVERE": "ERROR",
    "FATAL": "FATAL", "CRITICAL": "FATAL", "EMERGENCY": "FATAL", "EMERG": "FATAL", "ALERT": "FATAL", "PANIC": "FATAL",
}
NUMERIC_LEVELS = {10: "TRACE", 20: "DEBUG", 30: "INFO", 40: "WARN", 50: "ERROR", 60: "FATAL"}  # pino / bunyan

# Timestamps outside these years are treated as noise, never as the event time.
MIN_YEAR, MAX_YEAR = 2000, 2100
MAX_TIMESTAMP_CHARS = 64
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_EPOCH_LOW = int((datetime(MIN_YEAR, 1, 1, tzinfo=timezone.utc) - _UNIX_EPOCH).total_seconds())
_EPOCH_HIGH = int((datetime(MAX_YEAR + 1, 1, 1, tzinfo=timezone.utc) - _UNIX_EPOCH).total_seconds())
# The two ranges do not overlap, so the magnitude alone tells seconds from milliseconds.
_EPOCH_UNITS = (("seconds", 1, "epoch_seconds"), ("milliseconds", 1000, "epoch_millis"))

ISO_TIMESTAMP = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[Tt ](?P<h>\d{2}):(?P<mi>\d{2})"
    r"(?::(?P<s>\d{2})(?:[.,](?P<f>\d{1,9}))?)?"
    r" ?(?:(?P<z>[Zz])|(?P<sign>[+-])(?P<oh>\d{2}):?(?P<om>\d{2}))?(?!\d)"
)
# "java.lang.IllegalStateException: boom", "ValueError: x", "MyThrowable" - at the start of a line. The class name is
# bounded, so a megabyte of letters fails fast instead of being scanned for a suffix.
EXCEPTION_LINE = re.compile(r"^[ \t]*([\w.$]{1,500}(?:Exception|Error|Throwable))(?::(.*))?$", re.MULTILINE)


def normalise_level(value: str) -> str | None:
    return LEVELS.get(value.strip().upper())


def parse_timestamp(value: str, tz_name: str) -> tuple[datetime, str | None] | None:
    """A whole value that is an ISO-8601 timestamp, as aware UTC plus a provenance note."""
    value = value.strip()
    if len(value) > MAX_TIMESTAMP_CHARS:
        return None
    match = ISO_TIMESTAMP.fullmatch(value)
    return timestamp_from_match(match, tz_name) if match else None


def timestamp_from_match(match: re.Match[str], tz_name: str) -> tuple[datetime, str | None] | None:
    """A naive timestamp is read in the source's timezone and noted as "tz_assumed"; impossible dates give None."""
    if not MIN_YEAR <= int(match["y"]) <= MAX_YEAR:
        return None
    fraction = (match["f"] or "")[:6].ljust(6, "0")  # nanoseconds are cut to microseconds
    try:
        naive = datetime(
            int(match["y"]), int(match["mo"]), int(match["d"]), int(match["h"]), int(match["mi"]),
            int(match["s"] or 0), int(fraction),
        )
        zone = _offset(match)
    except ValueError:
        return None
    if zone is None:
        return naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc), "tz_assumed"
    return naive.replace(tzinfo=zone).astimezone(timezone.utc), None


def _offset(match: re.Match[str]) -> timezone | None:
    if match["z"]:
        return timezone.utc
    if not match["sign"]:
        return None
    hours, minutes = int(match["oh"]), int(match["om"])
    if hours > 23 or minutes > 59:
        raise ValueError("offset out of range")
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(-delta if match["sign"] == "-" else delta)


def from_epoch(value: object) -> tuple[datetime, str] | None:
    """Epoch seconds or milliseconds within MIN_YEAR..MAX_YEAR. Only call it for a key known to hold a timestamp."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    for unit, scale, note in _EPOCH_UNITS:
        if _EPOCH_LOW * scale <= value < _EPOCH_HIGH * scale:  # compared before any arithmetic, so it cannot overflow
            return _UNIX_EPOCH + timedelta(**{unit: value}), note
    return None
