"""Projects: read-only views of the project folders under LOGS_ROOT, and chat about one."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import Agent, ask
from app.controllers.dependencies import get_agent, get_project, get_projects, get_usable_project
from app.core.database import get_session
from app.ingestion.coverage import coverage_for_all, file_stats, orphan_ids, time_spans
from app.ingestion.projects import Project
from app.ingestion.sources import FileSource
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.projects import ProjectDetail, ProjectFile, ProjectSummary

router = APIRouter(prefix="/projects", tags=["Projects"])

Spans = dict[str, tuple[datetime, datetime]]


@router.get("", response_model=list[ProjectSummary], summary="All projects, with how much of each is indexed")
async def list_projects(
    session: Annotated[AsyncSession, Depends(get_session)],
    projects: Annotated[tuple[Project, ...], Depends(get_projects)],
) -> list[ProjectSummary]:
    spans = await time_spans(session, [f for p in projects for f in p.files])
    return [_summary(project, spans) for project in projects]


@router.get("/{project_id}", response_model=ProjectDetail, summary="One project")
async def get_project_detail(
    session: Annotated[AsyncSession, Depends(get_session)], project: Annotated[Project, Depends(get_project)],
) -> ProjectDetail:
    summary = _summary(project, await time_spans(session, project.files))
    return ProjectDetail(
        **summary.model_dump(), description=project.description, timezone=project.timezone, include=project.include)


@router.get("/{project_id}/files", response_model=list[ProjectFile], summary="A project's log files and their state")
async def list_files(
    session: Annotated[AsyncSession, Depends(get_session)], project: Annotated[Project, Depends(get_project)],
) -> list[ProjectFile]:
    missing = await orphan_ids(session, project)
    coverage = await coverage_for_all(session, project.files)
    stats = await file_stats(session, [*(f.id for f in project.files), *missing])
    disk = await asyncio.to_thread(lambda: [_stat(f) for f in project.files])
    rows = [
        _file_row(source.id, source.path.name, c.status, c.last_error, stats.get(source.id), size, mtime)
        for source, c, (size, mtime) in zip(project.files, coverage, disk)
    ]
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


def _summary(project: Project, spans: Spans) -> ProjectSummary:
    mine = [spans[f.id] for f in project.files if f.id in spans]
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

