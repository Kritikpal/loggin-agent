"""Turns one sanitised, redacted event into a ParsedEvent, its time, where each field came from, and its other fields.

Pure, deterministic and bounded: no LLM, no I/O. It never raises and never drops an event: whatever goes wrong, the
event comes back as raw text with a "_resolver" provenance entry naming the failure class (never the content).
Per field, earlier methods win: format parser, configured mappings, key names, values.
"""

from app.ingestion.fields.json_fields import resolve_object
from app.ingestion.fields.json_tree import FIELD_LIMITS, PARSE_LIMITS, clean, index_paths, parse_object, scalar_view, without
from app.ingestion.fields.model import TEXT_SOURCE, Found, Provenance, ResolvedEvent
from app.ingestion.fields.text_layouts import resolve_text_event
from app.ingestion.semantic_text import ParsedEvent
from app.ingestion.sources import SourceBase

FALLBACK_KEY = "_resolver"


def resolve(text: str, source: SourceBase) -> ResolvedEvent:
    try:
        if source.format == "json":
            return _resolve_json(text, source)
        found, fields = resolve_text_event(text, source.timezone)
        return _build(text, found, fields, {})  # pid/thread are layout noise for search: in `fields`, not `extra`
    except Exception as exc:  # untrusted input must never cost the event: store it raw and say why
        return fallback(text, type(exc).__name__)


def fallback(text: str, reason: str) -> ResolvedEvent:
    return ResolvedEvent(ParsedEvent(raw_text=text), provenance={FALLBACK_KEY: Provenance.of("fallback", TEXT_SOURCE, reason)})


def _resolve_json(text: str, source: SourceBase) -> ResolvedEvent:
    tree = parse_object(text)
    if tree is None:
        return fallback(text, "not_a_json_object")
    cleaned = clean(tree, PARSE_LIMITS)
    found, used = resolve_object(index_paths(cleaned), source.field_mappings, source.timezone)
    fields = clean(without(cleaned, used), FIELD_LIMITS)
    return _build(text, found, fields, scalar_view(fields))


def _build(text: str, found: Found, fields: dict, extra: dict) -> ResolvedEvent:
    values = {name: value for name, (value, _) in found.items() if name != "occurred_at"}
    occurred = found.get("occurred_at")
    return ResolvedEvent(
        parsed=ParsedEvent(raw_text=text, extra=extra, **values),
        occurred_at=occurred[0] if occurred else None,
        provenance={name: provenance for name, (_, provenance) in found.items()},
        fields=fields,
    )
