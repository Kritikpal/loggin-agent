"""The search tool's collaborators as FastAPI dependencies, so tests can swap them via app.dependency_overrides."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.core.embeddings import embed_query
from app.ingestion.coverage import time_spans
from app.ingestion.sources import Source, load_sources


def get_sources() -> tuple[Source, ...]:
    """The allowlist, read per request so edits to sources.yaml apply without a restart. A missing file = no sources."""
    return load_sources(get_settings().sources_config_path)


def get_embedder() -> Callable[[str], list[float]]:
    return embed_query


async def get_agent(session: Annotated[AsyncSession, Depends(get_session)]):
    """The investigation agent, built per request so edits to sources.yaml and newly indexed events reach its prompt.
    Needs GROQ_API_KEY."""
    from app.agent.graph import build_agent  # deferred: the agent module imports get_sources from this file

    sources = get_sources()
    return build_agent(sources=sources, spans=await time_spans(session, sources))
