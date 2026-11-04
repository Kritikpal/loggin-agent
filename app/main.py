import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.controllers.router import api_router
from app.core.config import get_settings
from app.core.database import SessionLocal, engine
from app.core.errors import register_exception_handlers
from app.ingestion.embed_loop import run_embed_loop


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    embedder = asyncio.create_task(run_embed_loop(SessionLocal, interval=get_settings().push_embed_interval_seconds))
    try:
        yield
    finally:
        embedder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await embedder
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Local AI Log Monitor",
        version="0.1.0",
        description=(
            "Local-first, read-only assistant that investigates application failures from local log files. "
            "Errors use `{\"error\": {\"code\", \"message\", \"details\"}}`. Search and sources are real; the AI chat "
            "endpoints are mocks for now."
        ),
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
