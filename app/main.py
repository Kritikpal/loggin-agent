import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.ai.graphs.checkpointer import open_checkpointer
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.database import engine
from app.core.errors import register_exception_handlers

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with AsyncExitStack() as stack:
        try:
            app.state.checkpointer = await stack.enter_async_context(open_checkpointer(get_settings().database_url))
        except Exception:  # only the AI workflows depend on it: keep the rest of the API up
            logger.exception("AI workflow storage is unavailable; /ai endpoints will answer 503 until restart")
            app.state.checkpointer = None
        yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Business Operator - Clothing Retail API",
        version="0.1.0",
        description=(
            "Phase 1 business APIs: products, inventory, sales, customers and draft campaigns. "
            "Schema is managed by Alembic. Errors use `{\"error\": {\"code\", \"message\", \"details\"}}`. "
            "Monetary values are exact decimal strings."
        ),
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
