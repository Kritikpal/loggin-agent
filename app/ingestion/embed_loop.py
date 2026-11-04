"""Gives pushed events their embeddings: the ingest endpoint stores rows without vectors so it can answer at once.

Runs for the life of the app (see app.main.lifespan). One pass is reembed_missing, which fills rows in chunks and
stops at the first failing chunk; the loop then sleeps and tries again, so a restarting database or a failing
embedder only delays the vectors. Failures are logged by exception class name only, never with log content.
"""

import asyncio
import logging

from app.core.embeddings import embed_documents
from app.ingestion.pipeline import Embedder, SessionFactory, reembed_missing

logger = logging.getLogger(__name__)


async def run_embed_loop(
    session_factory: SessionFactory, *, interval: float, embed: Embedder = embed_documents,
) -> None:
    """Until cancelled. ponytail: one loop per process (extra workers repeat harmless idempotent updates); a row that
    always makes the embedder fail is retried first on every pass and holds back the vectors after it."""
    while True:
        try:
            await reembed_missing(session_factory, embed=embed)
        except Exception as exc:  # a failed pass must never end the loop; CancelledError is not an Exception
            logger.warning("Embedding pass failed: %s", type(exc).__name__)
        await asyncio.sleep(interval)
