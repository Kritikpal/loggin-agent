"""Finds the canonical fields in a cleaned JSON object: configured mappings first, then well-known key names."""

from collections.abc import Callable, Mapping

from app.ingestion.fields.json_tree import Path
from app.ingestion.fields.model import Found, Method, Provenance
from app.ingestion.fields.values import (
    EXCEPTION_LINE, NUMERIC_LEVELS, from_epoch, normalise_level, parse_timestamp,
)

_CONTEXTS = ("", "mdc.", "context.")  # MDC-style objects that carry the correlation ids
# Lower-cased dotted paths, in priority order per field.
ALIASES: Mapping[str, tuple[str, ...]] = {
    "occurred_at": ("timestamp", "@timestamp", "time", "ts", "asctime", "datetime", "date", "t"),
    "level": ("level", "log.level", "severity", "severity_text", "levelname", "loglevel", "log_level", "lvl"),
    "message": ("message", "msg", "log", "text", "event"),
    "logger": ("logger", "logger_name", "log.logger", "loggername", "name"),
    "exception_type": ("exception.class", "exception.type", "error.type", "err.type", "@class"),
    "exception_message": ("exception.message", "error.message", "err.message"),
    "stack_trace": (
        "stack_trace", "stacktrace", "exception.stacktrace", "exception.stack_trace", "error.stack_trace",
        "error.stack", "err.stack",
    ),
    "trace_id": tuple(c + k for c in _CONTEXTS for k in ("trace_id", "traceid", "trace.id", "trace-id", "x-trace-id")),
    "request_id": tuple(
        c + k for c in _CONTEXTS
        for k in ("request_id", "requestid", "req_id", "x-request-id", "correlation_id", "correlationid")
    ),
}
EXCEPTION_STRING_KEYS = ("exception", "error", "err")  # a "Type: message" string here is split
NUMERIC_LEVEL_KEYS = frozenset({"level", "lvl", "severity"})  # only these may carry a pino/bunyan number

Converted = tuple[object, str | None] | None
Index = Mapping[str, tuple[Path, object]]


def _text(value: object, tz_name: str, numeric_ok: bool) -> Converted:
    if isinstance(value, str) and value.strip():
        return value.strip(), None
    if type(value) is int:  # numeric ids are common; bools are not ids
        return str(value), None
    return None


def _level(value: object, tz_name: str, numeric_ok: bool) -> Converted:
    if isinstance(value, str):
        level = normalise_level(value)
        return (level, None) if level else None
    if numeric_ok and type(value) is int and value in NUMERIC_LEVELS:
        return NUMERIC_LEVELS[value], "numeric_level"
    return None


def _time(value: object, tz_name: str, numeric_ok: bool) -> Converted:
    # Numbers are only ever looked at here, under a key that names a timestamp.
    return parse_timestamp(value, tz_name) if isinstance(value, str) else from_epoch(value)


CONVERTERS: Mapping[str, Callable[[object, str, bool], Converted]] = {
    name: {"occurred_at": _time, "level": _level}.get(name, _text) for name in ALIASES
}


def resolve_object(index: Index, mappings: Mapping[str, str], tz_name: str) -> tuple[Found, frozenset[Path]]:
    """The canonical fields found, and the paths they were taken from (left out of the stored fields)."""
    found: Found = {}
    used: set[Path] = set()
    for name, key in sorted(mappings.items()):  # a mapping to an unknown field name is ignored
        if name in CONVERTERS:
            _take(found, used, name, index.get(key.lower()), tz_name, "mapping", numeric_ok=True)
    for name, aliases in ALIASES.items():
        for alias in aliases:
            if name in found:
                break
            _take(found, used, name, index.get(alias), tz_name, "name", numeric_ok=alias in NUMERIC_LEVEL_KEYS)
    _split_exception_string(found, used, index)
    _drop_ambiguous_logger(found, used, index)
    return found, frozenset(used)


def _drop_ambiguous_logger(found: Found, used: set[Path], index: Index) -> None:
    """`name` is a logger only in a logging record; in {"name": "Alice", ...} it is data and stays in the fields."""
    logger = found.get("logger")
    if logger is not None and logger[1].source.lower() == "name" and "level" not in found:
        del found["logger"]
        used.discard(index["name"][0])


def _take(found: Found, used: set[Path], name: str, hit: tuple[Path, object] | None, tz_name: str, method: Method,
          numeric_ok: bool) -> None:
    if hit is None or name in found:
        return
    path, value = hit
    converted = CONVERTERS[name](value, tz_name, numeric_ok)
    if converted is None:  # present but unusable: it stays in the stored fields, and later methods may still fill it
        return
    result, note = converted
    if note == "numeric_level" and method == "name":
        method = "value"  # a bare number is weaker evidence than a level name
    found[name] = (result, Provenance.of(method, ".".join(path), note))
    used.add(path)


def _split_exception_string(found: Found, used: set[Path], index: Index) -> None:
    """"exception": "java.io.IOException: disk full\\n\\tat ..." gives type, message and (multi-line) the trace."""
    if "exception_type" in found:
        return
    for key in EXCEPTION_STRING_KEYS:
        path, value = index.get(key, ((), None))
        match = EXCEPTION_LINE.match(value.strip()) if isinstance(value, str) else None
        if match is None:
            continue
        provenance = Provenance.of("name", ".".join(path), "split")
        found["exception_type"] = (match[1], provenance)
        if match[2] and match[2].strip():
            found.setdefault("exception_message", (match[2].strip(), provenance))
        if "\n" in value.strip():
            found.setdefault("stack_trace", (value.strip(), provenance))
        used.add(path)
        return
