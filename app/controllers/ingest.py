"""Push ingestion: applications send their logs here instead of the server reading files. Upload is the same for a
whole log file, parsed into events on the way in."""

import asyncio
import logging
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.controllers.dependencies import get_project
from app.core.config import get_settings
from app.core.database import get_session
from app.core.errors import NotFoundError, ProjectInvalidError, StorageUnavailableError
from app.ingestion.projects import NOT_FOUND, Project
from app.ingestion.push import build_push_rows, check_token, parse_body, read_body
from app.ingestion.sources import PushSource
from app.ingestion.store import EventRow, insert_new_events
from app.ingestion.upload import build_upload_rows
from app.schemas.ingest import IngestResponse, PushEvent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["Ingest"])

UPLOAD_BATCH = 5000  # rows per INSERT; all batches of one upload share a transaction


_EVENT_SCHEMA = PushEvent.model_json_schema()
_EXAMPLE = [
    {"message": "TID: [-1234] [] [2026-11-04 10:00:00,123] ERROR {org.acme.Pool} - pool exhausted"},
    {"message": "payment failed", "timestamp": "2026-11-04T10:00:05Z", "level": "error"},
]
# The body is read by hand (so its size can be capped while streaming), which hides it from FastAPI: describe it here.
_BODY_DOCS = {"requestBody": {"required": True, "content": {
    "application/json": {
        "schema": {"type": "array", "items": _EVENT_SCHEMA}, "example": _EXAMPLE},
    "application/x-ndjson": {"schema": _EVENT_SCHEMA, "example": _EXAMPLE[0]},
}}}
_UPLOAD_DOCS = {"requestBody": {"required": True, "content": {
    "text/plain": {"schema": {"type": "string"}, "example": _EXAMPLE[0]["message"]}}}}


@router.post(
    "/{project_id}/ingest", response_model=IngestResponse, summary="Push log events into a project",
    openapi_extra=_BODY_DOCS)
async def ingest(
    request: Request,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header(description="Bearer <the project's push token>")] = None,
) -> IngestResponse:
    source = _authorized_source(project, authorization)
    events = parse_body(await read_body(request), request.headers.get("content-type", ""))
    rows = await asyncio.to_thread(build_push_rows, events, source)
    stored = await _store(session, source, rows)
    skipped = len(events) - len(rows)
    if skipped:  # one line per request, with the count only: log content stays out of the logs
        logger.warning("Skipped %d blank events pushed to %s", skipped, source.id)
    return IngestResponse(received=len(events), stored=stored, duplicates=len(rows) - stored, skipped=skipped)


@router.post(
    "/{project_id}/upload", response_model=IngestResponse, summary="Upload a whole log file into a project",
    openapi_extra=_UPLOAD_DOCS)
async def upload(
    request: Request,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header(description="Bearer <the project's push token>")] = None,
) -> IngestResponse:
    """The body is the raw text of a log file (`curl --data-binary @app.log`). It is split into events using the
    project's event pattern; the file itself is not kept, only its events."""
    source = _authorized_source(project, authorization)
    body = await read_body(request, limit=get_settings().upload_max_body_bytes, setting="UPLOAD_MAX_BODY_BYTES")
    rows = await asyncio.to_thread(build_upload_rows, body, source)
    stored = await _store(session, source, rows, batch=UPLOAD_BATCH)
    return IngestResponse(received=len(rows), stored=stored, duplicates=len(rows) - stored, skipped=0)


def _authorized_source(project: Project, authorization: str | None) -> PushSource:
    source = project.push_source
    if source is None:
        raise NotFoundError(NOT_FOUND)
    if get_settings().ingest_require_token:
        check_token(source, authorization)
    else:
        logger.warning("INGEST_REQUIRE_TOKEN is off: accepted a push to %s without a token check", project.id)
    if project.error:  # only after the token: a caller without it learns nothing about the project
        raise ProjectInvalidError(f"Project '{project.id}' is invalid.")
    return source


async def _store(session: AsyncSession, source: PushSource, rows: Sequence[EventRow], *, batch: int = 0) -> int:
    """Insert the new rows in one transaction; returns how many were really new."""
    size = batch or max(len(rows), 1)
    try:
        async with session.begin():
            return sum([await insert_new_events(session, source.id, rows[i:i + size]) for i in range(0, len(rows), size)])
    except (OperationalError, InterfaceError, PoolTimeoutError) as exc:
        # the database is unreachable: retry. Any other database error is a bug and stays a 500.
        # The class name only: the message can carry log content.
        logger.warning("Storing pushed events of %s failed: %s", source.id, type(exc).__name__)
        raise StorageUnavailableError("The log store is unavailable; retry later.") from None
