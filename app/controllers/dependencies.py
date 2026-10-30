"""The search tool's collaborators as FastAPI dependencies, so tests can swap them via app.dependency_overrides."""

from collections.abc import Callable

from app.core.config import get_settings
from app.core.embeddings import embed_query
from app.ingestion.sources import Source, load_sources


def get_sources() -> tuple[Source, ...]:
    """The allowlist, read per request so edits to sources.yaml apply without a restart. A missing file = no sources."""
    return load_sources(get_settings().sources_config_path)


def get_embedder() -> Callable[[str], list[float]]:
    return embed_query
