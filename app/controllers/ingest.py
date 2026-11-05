"""Push ingestion: applications send their logs here instead of the server reading files."""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.controllers.dependencies import get_project
from app.core.database import get_session
from app.core.errors import NotFoundError, ProjectInvalidError, StorageUnavailableError
from app.ingestion.projects import NOT_FOUND, Project
from app.ingestion.push import build_push_rows, check_token, parse_body, read_body
from app.ingestion.store import insert_new_events
from app.schemas.ingest import IngestResponse, PushEvent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["Ingest"])


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


@router.post(
    "/{project_id}/ingest", response_model=IngestResponse, summary="Push log events into a project",
    openapi_extra=_BODY_DOCS)
async def ingest(
    request: Request,
    project: Annotated[Project, Depends(get_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header(description="Bearer <the project's push token>")] = None,
) -> IngestResponse:
    source = project.push_source
    if source is None:
        raise NotFoundError(NOT_FOUND)
    check_token(source, authorization)
    if project.error:  # only after the token: a caller without it learns nothing about the project
        raise ProjectInvalidError(f"Project '{project.id}' is invalid.")
    events = parse_body(await read_body(request), request.headers.get("content-type", ""))
    rows = await asyncio.to_thread(build_push_rows, events, source)
    try:
        async with session.begin():
            stored = await insert_new_events(session, source.id, rows)
    except (OperationalError, InterfaceError, PoolTimeoutError) as exc:
        # the database is unreachable: retry. Any other database error is a bug and stays a 500.
        # The class name only: the message can carry log content.
        logger.warning("Storing pushed events of %s failed: %s", source.id, type(exc).__name__)
        raise StorageUnavailableError("The log store is unavailable; retry later.") from None
    skipped = len(events) - len(rows)
    if skipped:  # one line per request, with the count only: log content stays out of the logs
        logger.warning("Skipped %d blank events pushed to %s", skipped, source.id)
    return IngestResponse(received=len(events), stored=stored, duplicates=len(rows) - stored, skipped=skipped)
