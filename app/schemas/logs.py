from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class LogItem(BaseModel):
    """One event in the list. `text` is cut at 2000 characters; `truncated` says so, the detail has the whole text."""

    id: UUID
    source: str
    occurred_at: datetime | None
    level: str | None
    text: str
    truncated: bool
    signature: str | None
    actor: str | None  # the audit initiator, when the line is an audit event
    action: str | None  # the audit action, e.g. Login


class LogPage(BaseModel):
    items: list[LogItem]
    next_cursor: str | None


class LogDetail(BaseModel):
    id: UUID
    source: str
    locator: dict[str, Any]
    occurred_at: datetime | None
    level: str | None
    raw_text: str
    fields: dict[str, Any]
    signature: str | None
    signature_template: str | None


class StatsBucket(BaseModel):
    start: datetime
    total: int
    by_level: dict[str, int]


class LogStats(BaseModel):
    interval: str
    sampled: bool  # the window held more than the read cap: only its newest events are counted
    buckets: list[StatsBucket]


class LevelFacet(BaseModel):
    value: str
    count: int


class SignatureFacet(BaseModel):
    value: str
    template: str | None
    count: int


class LogFacets(BaseModel):
    sampled: bool
    levels: list[LevelFacet]
    signatures: list[SignatureFacet]
