"""Pushed logs: reading the request body, checking the token and turning events into rows for the store.

Every event is redacted first and everything stored or hashed derives from the redacted text, so a secret never
reaches the database or the dedup key. Errors never echo log content.
"""

import hashlib
import hmac
import json
import os
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import Request
from pydantic import ValidationError

from app.core.errors import InvalidPayloadError, NotFoundError, PayloadTooLargeError, UnauthorizedError
from app.ingestion.assembler import RawEvent
from app.ingestion.pipeline import prepare_event
from app.ingestion.projects import NOT_FOUND
from app.ingestion.readers.base import SourceEvent
from app.ingestion.redaction import redact
from app.ingestion.sanitize import sanitize
from app.ingestion.sources import PushSource
from app.ingestion.store import EventRow
from app.schemas.ingest import PushEvent

MAX_BODY_BYTES = 1024 * 1024
MAX_EVENTS = 1000
MAX_LEVEL_CHARS = 32
LOCATOR = {"kind": "push"}


def check_token(source: PushSource, authorization: str | None) -> None:
    """404 when the project has no token configured (indistinguishable from an unknown project), 401 when the
    request's bearer token is missing or wrong."""
    expected = os.environ.get(source.token_env, "")
    if not expected:
        raise NotFoundError(NOT_FOUND)
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise UnauthorizedError("Missing or invalid token.")


async def read_body(request: Request) -> bytes:
    """The body, stopping as soon as it is over the limit instead of buffering what a client keeps sending."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise PayloadTooLargeError(f"The body is over {MAX_BODY_BYTES} bytes.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise PayloadTooLargeError(f"The body is over {MAX_BODY_BYTES} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_body(raw: bytes, content_type: str) -> list[PushEvent]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidPayloadError("The body must be UTF-8 text.") from None
    items = _json_items(text, ndjson="ndjson" in content_type.lower())
    if len(items) > MAX_EVENTS:
        raise PayloadTooLargeError(f"At most {MAX_EVENTS} events per request.")
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
    """CPU-bound (redaction, parsing): call it from a worker thread."""
    return [_row(event, source) for event in events]


def _row(event: PushEvent, source: PushSource) -> EventRow:
    text = redact(sanitize(event.message).strip("\n")).text
    raw = RawEvent(text, 0, 0, 1, 1 + text.count("\n"), False)
    row = prepare_event(SourceEvent(raw, push_event_key(text, event.timestamp), LOCATOR), source)
    sent_at = _sender_time(event.timestamp, source.timezone)
    sent_level = (event.level or "").strip().upper()[:MAX_LEVEL_CHARS] or None
    return replace(row, occurred_at=sent_at or row.occurred_at, level=row.level or sent_level)


def _sender_time(value: str | None, timezone: str) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=ZoneInfo(timezone))).astimezone(UTC)
