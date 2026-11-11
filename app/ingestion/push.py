"""Pushed logs: reading the request body and turning events into rows for the store.

Every event is redacted first and everything stored or hashed derives from the redacted text, so a secret never
reaches the database or the dedup key. Errors never echo log content.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import Request
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.errors import InvalidPayloadError, PayloadTooLargeError
from app.ingestion.assembler import RawEvent
from app.ingestion.pipeline import prepare_event
from app.ingestion.readers.base import SourceEvent
from app.ingestion.pseudonym import configured_pseudonym_key
from app.ingestion.redaction import redact
from app.ingestion.sanitize import sanitize
from app.ingestion.sources import PushSource
from app.ingestion.store import EventRow
from app.schemas.ingest import PushEvent

MAX_MESSAGE_CHARS = 256 * 1024  # the same cap a file event gets (AssemblyLimits.max_chars)
# The level names the agent ranks (app.agent.tools.common.LEVEL_RANKS); any other level a sender makes up is dropped.
SENDER_LEVELS = frozenset({"TRACE", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "SEVERE", "FATAL", "CRITICAL"})
LOCATOR = {"kind": "push"}


async def read_body(request: Request, *, limit: int | None = None, setting: str = "PUSH_MAX_BODY_BYTES") -> bytes:
    """The body, stopping as soon as it is over the limit instead of buffering what a client keeps sending."""
    limit = limit or get_settings().push_max_body_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError(f"The body is over {limit} bytes ({setting}).")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLargeError(f"The body is over {limit} bytes ({setting}).")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_body(raw: bytes, content_type: str) -> list[PushEvent]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidPayloadError("The body must be UTF-8 text.") from None
    items = _json_items(text, ndjson="ndjson" in content_type.lower())
    limit = get_settings().push_max_events
    if len(items) > limit:
        raise PayloadTooLargeError(f"At most {limit} events per request (PUSH_MAX_EVENTS).")
    try:
        return [PushEvent.model_validate(item) for item in items]
    except ValidationError as exc:  # details name the fields only, never the values
        details = [{"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in exc.errors()]
        raise InvalidPayloadError("Each event needs a non-blank string 'message'.", details) from None


def _json_items(text: str, *, ndjson: bool) -> list:
    try:
        if ndjson:
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        items = json.loads(text)
    except ValueError:
        raise InvalidPayloadError("The body is not valid JSON.") from None
    if not isinstance(items, list):
        raise InvalidPayloadError("The body must be a JSON list of events.")
    return items


def push_event_key(redacted_text: str, sender_timestamp: str | None) -> str:
    digest = hashlib.sha256(f"{redacted_text}\n{sender_timestamp or ''}".encode()).hexdigest()
    return f"push:{digest}"


def build_push_rows(events: Sequence[PushEvent], source: PushSource) -> list[EventRow]:
    """One row per event with text; blank events (nothing left once control characters go) give no row.
    CPU-bound (redaction, parsing): call it from a worker thread."""
    rows = (_row(event, source) for event in events)
    return [row for row in rows if row is not None]


def _row(event: PushEvent, source: PushSource) -> EventRow | None:
    cleaned = sanitize(event.message).strip("\n")
    if not cleaned.strip():
        return None
    text = redact(cleaned, pseudonym_key=configured_pseudonym_key()).text[:MAX_MESSAGE_CHARS]  # clip after redaction: a cut secret may escape it
    raw = RawEvent(text, 0, 0, 1, 1 + text.count("\n"), False)
    row = prepare_event(SourceEvent(raw, push_event_key(text, event.timestamp), LOCATOR), source)
    sent_at = _sender_time(event.timestamp, source.timezone)
    sent_level = (event.level or "").strip().upper()
    sent_level = sent_level if sent_level in SENDER_LEVELS else None
    return replace(row, occurred_at=sent_at or row.occurred_at, level=row.level or sent_level)


def _sender_time(value: str | None, timezone: str) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
        return (moment if moment.tzinfo else moment.replace(tzinfo=ZoneInfo(timezone))).astimezone(UTC)
    except (ValueError, OverflowError):  # unparseable, or so near the date range edge that the zone shift overflows
        return None
