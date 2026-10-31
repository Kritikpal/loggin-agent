"""search_logs: read-only hybrid search over indexed log events. No LLM.

Two legs find candidates independently, each with the request's filters inside its own SQL:
  semantic  cosine distance between the question's embedding and each event's embedding (rows without one skipped)
  keyword   full-text match of the question's lexemes on search_tsv, OR-style (see app.tools.keyword_query)
and reciprocal rank fusion merges them: score = sum over legs of weight / (RRF_K + rank in that leg). There is no
similarity cutoff, so any question gets the nearest events; `matched_by` says which legs found each hit.

If the embedder fails or returns an unusable vector the search degrades to keyword-only instead of failing.
"""

import asyncio
import logging
import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embeddings import EMBEDDING_DIMENSIONS, embed_query
from app.ingestion.coverage import coverage_for_all
from app.ingestion.sources import Source, get_source
from app.schemas.search_logs import LogFilters, SearchHit, SearchLogsRequest, SearchLogsResponse
from app.tools.keyword_query import keyword_lexemes, tsquery_text
from app.tools.search_queries import Candidate, fetch_events, filters, keyword_candidates, semantic_candidates

logger = logging.getLogger(__name__)

Embedder = Callable[[str], list[float]]
MIN_CANDIDATES = 50  # each leg fetches at least this many, so fusion sees more than the final top_k
RRF_K = 60
LEG_WEIGHTS = {"semantic": 1.0, "keyword": 1.0}


@dataclass(frozen=True)
class Fused:
    id: uuid.UUID
    score: float
    occurred_ts: float  # for the tie-break; events without a time sort last
    event_key: str
    matched_by: tuple[str, ...]


async def search_logs(
    request: SearchLogsRequest, session: AsyncSession, *, sources: tuple[Source, ...], embed: Embedder = embed_query,
) -> SearchLogsResponse:
    searched = searched_sources(request, sources)
    where = filters(request, [source.id for source in searched])
    pool = max(request.top_k, MIN_CANDIDATES)
    legs: dict[str, list[Candidate]] = {}
    vector = await _query_vector(embed, request.query)
    if vector is not None:
        legs["semantic"] = await semantic_candidates(session, vector, where, pool)
    tsquery = tsquery_text(keyword_lexemes(request.query))
    if tsquery is not None:
        legs["keyword"] = await keyword_candidates(session, tsquery, where, pool)
    ranked = fuse(legs)[:request.top_k]
    rows = await fetch_events(session, [f.id for f in ranked])
    results = [_hit(rows[f.id], f) for f in ranked if f.id in rows]
    return SearchLogsResponse(results=results, coverage=await coverage_for_all(session, searched))


def searched_sources(request: LogFilters, sources: tuple[Source, ...]) -> tuple[Source, ...]:
    """The allowlisted sources to search, in request order. Any id not on the allowlist refuses the whole request."""
    if request.source_ids is None:
        return sources
    return tuple(get_source(sources, source_id) for source_id in dict.fromkeys(request.source_ids))


async def _query_vector(embed: Embedder, query: str) -> list[float] | None:
    try:
        vector = [float(x) for x in await asyncio.to_thread(embed, query)]
    except Exception as exc:
        logger.warning("Embedding the search query failed, keyword-only search: %s", type(exc).__name__)
        return None
    if len(vector) != EMBEDDING_DIMENSIONS or not all(math.isfinite(x) for x in vector):
        logger.warning("Embedding the search query gave an unusable vector, keyword-only search")
        return None
    return vector


def fuse(legs: dict[str, Sequence[Candidate]], k: float = RRF_K) -> list[Fused]:
    """Reciprocal rank fusion. Order: score desc, then newest first, then event_key (deterministic)."""
    merged: dict[uuid.UUID, Fused] = {}
    for leg in ("semantic", "keyword"):
        for rank, candidate in enumerate(legs.get(leg, ()), 1):
            previous = merged.get(candidate.id)
            gain = LEG_WEIGHTS[leg] / (k + rank)
            if previous is None:
                ts = candidate.occurred_at.timestamp() if candidate.occurred_at else -math.inf
                merged[candidate.id] = Fused(candidate.id, gain, ts, candidate.event_key, (leg,))
            else:
                merged[candidate.id] = Fused(
                    candidate.id, previous.score + gain, previous.occurred_ts, previous.event_key,
                    (*previous.matched_by, leg))
    return sorted(merged.values(), key=lambda f: (-f.score, -f.occurred_ts, f.event_key))


def _hit(row: Row, fused: Fused) -> SearchHit:
    return SearchHit(
        event_id=row.id, source_id=row.source_id, occurred_at=row.occurred_at, level=row.level,
        locator=row.locator, raw_text=row.raw_text, score=fused.score, matched_by=list(fused.matched_by),
    )
