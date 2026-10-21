from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.database import engine
from app.core.errors import register_exception_handlers


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
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
