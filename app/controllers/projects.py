"""Projects, kept in the database: list and inspect them, create, edit and delete them (admin key), chat about one."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import Agent, ask
from app.controllers.dependencies import get_agent, get_project, get_projects, get_usable_project, require_admin
from app.core.database import get_session
from app.core.errors import ConflictError, InvalidPayloadError, NotFoundError
from app.ingestion.coverage import coverage_for_all, file_stats, orphan_ids, time_spans
from app.ingestion.projects import (
    NOT_FOUND, PROJECT_ID, Project, hash_token, new_token, project_from_config, row_doc,
)
from app.ingestion.sources import ConfigError, FileSource
from app.models import LogEvent, SourceIndexState
from app.models.base import utcnow
from app.models.project import ProjectRow
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.projects import (
    ProjectCreate, ProjectCreated, ProjectDetail, ProjectFile, ProjectSummary, ProjectUpdate, PushToken,
)

router = APIRouter(prefix="/projects", tags=["Projects"])

Spans = dict[str, tuple[datetime, datetime]]


@router.get("", response_model=list[ProjectSummary], summary="All projects, with how much of each is indexed")
async def list_projects(
    session: Annotated[AsyncSession, Depends(get_session)],
    projects: Annotated[tuple[Project, ...], Depends(get_projects)],
) -> list[ProjectSummary]:
    spans = await time_spans(session, [s for p in projects for s in p.sources])
    return [_summary(project, spans) for project in projects]


@router.get("/{project_id}", response_model=ProjectDetail, summary="One project")
async def get_project_detail(
    session: Annotated[AsyncSession, Depends(get_session)], project: Annotated[Project, Depends(get_project)],
) -> ProjectDetail:
    return await _detail(session, project)


@router.post(
    "", status_code=201, response_model=ProjectCreated, dependencies=[Depends(require_admin)],
    summary="Create a project (admin key); the reply carries its push token, shown once")
async def create_project(
    data: ProjectCreate, session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectCreated:
    if not PROJECT_ID.fullmatch(data.id):
        raise InvalidPayloadError(f"id must match {PROJECT_ID.pattern}")
    config = _config_of(data.model_dump(exclude_unset=True))
    token = new_token()
    row = ProjectRow(
        id=data.id, title=data.title, description=data.description, type=data.type, timezone=data.timezone,
        config=config, push_token_hash=hash_token(token))
    project = _validated(row)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError(f"A project '{data.id}' already exists.") from None
    return ProjectCreated(**(await _detail(session, project)).model_dump(), push_token=token)


@router.patch(
    "/{project_id}", response_model=ProjectDetail, dependencies=[Depends(require_admin)],
    summary="Edit a project (admin key); send only the fields to change")
async def update_project(
    project_id: str, data: ProjectUpdate, session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectDetail:
    row = await _row(session, project_id)
    changes = data.model_dump(exclude_unset=True)
    for key in ("title", "type", "description", "timezone"):
        if changes.get(key) is not None:
            setattr(row, key, changes[key])
    config = dict(row.config)  # a new dict: JSONB changes are only noticed when the value is replaced
    for key, value in _config_of(changes, keep_none=True).items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    row.config, row.updated_at = config, utcnow()
    project = _validated(row)
    await session.commit()
    return await _detail(session, project)


@router.post(
    "/{project_id}/token", response_model=PushToken, dependencies=[Depends(require_admin)],
    summary="Replace a project's push token (admin key); the old one stops working")
async def rotate_token(project_id: str, session: Annotated[AsyncSession, Depends(get_session)]) -> PushToken:
    row = await _row(session, project_id)
    token = new_token()
    row.push_token_hash, row.updated_at = hash_token(token), utcnow()
    await session.commit()
    return PushToken(push_token=token)


@router.delete(
    "/{project_id}", status_code=204, dependencies=[Depends(require_admin)],
    summary="Delete a project and all its logs (admin key)")
async def delete_project(project_id: str, session: Annotated[AsyncSession, Depends(get_session)]) -> Response:
    row = await _row(session, project_id)
    # autoescape: '_' in a project id is not a LIKE wildcard, so 'shop_2' is never swept up with 'shop'
    prefix = f"{row.id}/"
    await session.execute(delete(LogEvent).where(LogEvent.source_id.startswith(prefix, autoescape=True)))
    await session.execute(delete(SourceIndexState).where(SourceIndexState.source_id.startswith(prefix, autoescape=True)))
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)


@router.get("/{project_id}/files", response_model=list[ProjectFile], summary="A project's log files and their state")
async def list_files(
    session: Annotated[AsyncSession, Depends(get_session)], project: Annotated[Project, Depends(get_project)],
) -> list[ProjectFile]:
    missing = await orphan_ids(session, project)
    coverage = await coverage_for_all(session, project.files)
    stats = await file_stats(session, [*(s.id for s in project.sources), *missing])
    disk = await asyncio.to_thread(lambda: [_stat(f) for f in project.files])
    rows = [
        _file_row(source.id, source.path.name, c.status, c.last_error, stats.get(source.id), size, mtime)
        for source, c, (size, mtime) in zip(project.files, coverage, disk)
    ]
    if push := project.push_source:
        rows.append(_file_row(push.id, "push", "complete", None, stats.get(push.id), None, None))
    rows += [_file_row(f"{project.id}/{s.name}", s.name, "skipped", s.reason, None, None, None) for s in project.skipped]
    rows += [_file_row(i, _short(i), "missing", None, stats.get(i), None, None) for i in missing]
    return rows


@router.post("/{project_id}/chat", response_model=ChatResponse, summary="Ask the log investigation agent about a project")
async def project_chat(
    data: ChatRequest,
    project: Annotated[Project, Depends(get_usable_project)],  # resolved even when a test swaps the agent
    agent: Annotated[Agent, Depends(get_agent)],
) -> ChatResponse:
    # ponytail: every question starts fresh; conversation_id is echoed, not remembered. Add a checkpointer when
    # follow-up questions are needed.
    reply = await ask(agent, data.message)
    return ChatResponse(
        response=reply.answer, tool_calls=reply.tool_calls, conversation_id=data.conversation_id or uuid.uuid4())


async def _detail(session: AsyncSession, project: Project) -> ProjectDetail:
    summary = _summary(project, await time_spans(session, project.sources))
    return ProjectDetail(
        **summary.model_dump(), description=project.description, timezone=project.timezone, include=project.include)


async def _row(session: AsyncSession, project_id: str) -> ProjectRow:
    row = await session.get(ProjectRow, project_id)
    if row is None:
        raise NotFoundError(NOT_FOUND)  # the same message for an unknown and a malformed id
    return row


def _config_of(fields: dict, *, keep_none: bool = False) -> dict:
    """The optional parsing settings among the given fields."""
    return {k: fields[k] for k in ("event_start", "app_packages", "field_mappings")
            if k in fields and (keep_none or fields[k] is not None)}


def _validated(row: ProjectRow) -> Project:
    """The project this row describes, or a 422 saying what is wrong with it."""
    try:
        return project_from_config(row.id, row_doc(row), token_hash=row.push_token_hash)
    except ConfigError as exc:
        raise InvalidPayloadError(exc.message) from None


def _summary(project: Project, spans: Spans) -> ProjectSummary:
    mine = [spans[s.id] for s in project.sources if s.id in spans]
    return ProjectSummary(
        id=project.id, title=project.title, type=project.type, status=project.status, file_count=len(project.files),
        indexed_from=min((s for s, _ in mine), default=None), indexed_to=max((e for _, e in mine), default=None),
        error=project.error)


def _file_row(file_id, name, status, last_error, stats, size, mtime) -> ProjectFile:
    return ProjectFile(
        id=_short(file_id), name=name, size_bytes=size, modified_at=mtime, status=status, last_error=last_error,
        event_count=stats.event_count if stats else 0, first_at=stats and stats.first_at,
        last_at=stats and stats.last_at, parsed_ratio=stats and stats.parsed_ratio)


def _short(file_id: str) -> str:
    return file_id.split("/", 1)[1]


def _stat(source: FileSource) -> tuple[int | None, datetime | None]:
    try:
        st = source.path.stat()
    except OSError:
        return None, None
    return st.st_size, datetime.fromtimestamp(st.st_mtime, UTC)

