from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.controllers.dependencies import get_embedder, get_sources
from app.core.database import get_session
from app.ingestion.sources import Source
from app.schemas.search_logs import SearchLogsRequest, SearchLogsResponse
from app.tools.search_logs import search_logs

router = APIRouter(prefix="/tools", tags=["Tools"])


@router.post("/search-logs", response_model=SearchLogsResponse, summary="Hybrid (semantic + keyword) log search")
async def search_logs_endpoint(
    request: SearchLogsRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    sources: Annotated[tuple[Source, ...], Depends(get_sources)],
    embed: Annotated[Callable[[str], list[float]], Depends(get_embedder)],
) -> SearchLogsResponse:
    return await search_logs(request, session, sources=sources, embed=embed)
