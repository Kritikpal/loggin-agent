"""Push ingestion: applications send their logs here instead of the server reading files."""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.controllers.dependencies import get_usable_project
from app.core.database import get_session
from app.core.errors import NotFoundError, StorageUnavailableError
from app.ingestion.projects import NOT_FOUND, Project
from app.ingestion.push import build_push_rows, check_token, parse_body, read_body
from app.ingestion.store import insert_new_events
from app.schemas.ingest import IngestResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["Ingest"])


@router.post("/{project_id}/ingest", response_model=IngestResponse, summary="Push log events into a project")
async def ingest(
    request: Request,
    project: Annotated[Project, Depends(get_usable_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IngestResponse:
    source = project.push_source
    if source is None:
        raise NotFoundError(NOT_FOUND)
    check_token(source, request.headers.get("authorization"))
    events = parse_body(await read_body(request), request.headers.get("content-type", ""))
    rows = await asyncio.to_thread(build_push_rows, events, source)
    try:
        async with session.begin():
            stored = await insert_new_events(session, source.id, rows)
    except SQLAlchemyError as exc:  # the class name only: the message can carry log content
        logger.warning("Storing pushed events of %s failed: %s", source.id, type(exc).__name__)
        raise StorageUnavailableError("The log store is unavailable; retry later.") from None
    return IngestResponse(received=len(rows), stored=stored, duplicates=len(rows) - stored)
