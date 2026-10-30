"""The field resolver's result types."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.ingestion.semantic_text import ParsedEvent

Method = Literal["parser", "mapping", "name", "value", "fallback"]
CONFIDENCE: Mapping[str, float] = {"parser": 1.0, "mapping": 1.0, "name": 0.9, "value": 0.6, "fallback": 0.0}
TEXT_SOURCE = "text"


@dataclass(frozen=True)
class Provenance:
    """How one field was found, so a later reader can weigh it. Never contains log content beyond key names."""

    method: Method
    confidence: float
    source: str  # the JSON key path the value came from, or "text"
    note: str | None = None  # e.g. "tz_assumed", "numeric_level", "epoch_millis", or the failure class on fallback

    @classmethod
    def of(cls, method: Method, source: str, note: str | None = None) -> "Provenance":
        return cls(method, CONFIDENCE[method], source, note)


@dataclass(frozen=True)
class ResolvedEvent:
    parsed: ParsedEvent
    occurred_at: datetime | None = None  # timezone-aware UTC
    provenance: Mapping[str, Provenance] = field(default_factory=dict)  # keyed by canonical field name
    fields: Mapping[str, Any] = field(default_factory=dict)  # the remaining dynamic fields, JSON-compatible, bounded


# canonical field name -> (value, how it was found); occurred_at holds a datetime, every other field a str
Found = dict[str, tuple[object, Provenance]]
