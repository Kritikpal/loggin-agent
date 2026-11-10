"""The project and agent as FastAPI dependencies, so tests can swap them via app.dependency_overrides."""

import hmac
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.core.errors import ProjectInvalidError, UnauthorizedError
from app.ingestion.coverage import time_spans
from app.ingestion.projects import Project, find_project, project_from_row
from app.models.project import ProjectRow


async def get_projects(session: Annotated[AsyncSession, Depends(get_session)]) -> tuple[Project, ...]:
    """Every project, read from the database per request so a change applies at once.
    ponytail: one small SELECT per request; cache it if it ever shows in latency."""
    rows = await session.scalars(select(ProjectRow).order_by(ProjectRow.id))
    projects = tuple(project_from_row(row) for row in rows)
    await session.rollback()  # ends this read's transaction: the write routes sharing the session start their own
    return projects


def require_admin(authorization: Annotated[str | None, Header(description="Bearer <ADMIN_API_KEY>")] = None) -> None:
    """Guards the routes that create, edit or delete projects. No ADMIN_API_KEY configured = they stay closed."""
    key = get_settings().admin_api_key
    scheme, _, supplied = (authorization or "").partition(" ")
    if not key or scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), key.encode()):
        raise UnauthorizedError("Missing or invalid admin key.")


def get_project(project_id: str, projects: Annotated[tuple[Project, ...], Depends(get_projects)]) -> Project:
    """Only looked up among the scanned projects: the id from the path never touches the filesystem."""
    return find_project(projects, project_id)


def get_usable_project(project: Annotated[Project, Depends(get_project)]) -> Project:
    """A project that can be searched or chatted with: one whose project.yaml is valid."""
    if project.error:
        raise ProjectInvalidError(f"Project '{project.id}' is invalid: {project.error}")
    return project


async def get_agent(
    project: Annotated[Project, Depends(get_usable_project)], session: Annotated[AsyncSession, Depends(get_session)],
):
    """The investigation agent for one project, built per request so newly copied or indexed files reach its prompt.
    Needs GROQ_API_KEY."""
    from app.agent.graph import build_agent  # deferred: importing the agent loads the model client

    return build_agent(project=project, spans=await time_spans(session, project.sources))
