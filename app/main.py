from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.controllers.router import api_router
from app.core.database import engine
from app.core.errors import register_exception_handlers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()
    from app.core.llm import close_http_client  # deferred, like the agent: the model client loads on first chat

    await close_http_client()


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
