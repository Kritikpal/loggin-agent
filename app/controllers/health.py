from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.controllers.dependencies import get_sources
from app.core.database import get_session
from app.ingestion.coverage import coverage_for_all
from app.ingestion.sources import Source

router = APIRouter(tags=["Health"])


class SourceInfo(BaseModel):
    id: str
    type: Literal["file"]
    format: Literal["text", "json"]
    status: Literal["complete", "stale", "partial", "not_indexed", "error"]
    indexed_through: datetime | None
    last_error: str | None


@router.get("/health", summary="Liveness check")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/sources", response_model=list[SourceInfo], summary="Configured log sources and how far each is indexed")
async def sources(
    session: Annotated[AsyncSession, Depends(get_session)],
    configured: Annotated[tuple[Source, ...], Depends(get_sources)],
) -> list[SourceInfo]:
    coverage = await coverage_for_all(session, configured)
    return [
        SourceInfo(id=source.id, type="file", format=source.format, status=c.status,
                   indexed_through=c.indexed_through, last_error=c.last_error)
        for source, c in zip(configured, coverage)
    ]
