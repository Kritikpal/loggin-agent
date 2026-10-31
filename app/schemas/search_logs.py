import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LogFilters(BaseModel):
    """The filters search_logs and summarize_logs share, so both tools always agree on what is in scope."""

    model_config = ConfigDict(extra="forbid")

    source_ids: list[str] | None = Field(default=None, description="Restrict to these approved log sources")
    start_time: datetime | None = Field(default=None, description="Earliest event time, ISO 8601 in UTC, e.g. 2026-03-10T10:14:00Z")
    end_time: datetime | None = Field(default=None, description="Latest event time, ISO 8601 in UTC, e.g. 2026-03-10T10:15:00Z")
    levels: list[str] | None = Field(default=None, description="ERROR, WARN, INFO, ...")

    @field_validator("levels")
    @classmethod
    def _upper_levels(cls, levels: list[str] | None) -> list[str] | None:
        return [level.strip().upper() for level in levels] if levels else levels

    @model_validator(mode="after")
    def _check_time_range(self) -> Self:
        if self.start_time and self.end_time and self.start_time > self.end_time:
            raise ValueError("start_time must not be after end_time")
        return self


class SearchLogsRequest(LogFilters):
    """Step 2 contract of the search_logs tool."""

    query: str = Field(min_length=1, max_length=2000, description="Natural-language search query")
    top_k: int = Field(default=10, ge=1, le=50, description="Maximum number of results")


class LevelCount(BaseModel):
    """How many events one source has at one level, and when the first and last of them happened."""

    source_id: str
    level: str | None
    count: int
    first_at: datetime | None
    last_at: datetime | None


class SearchHit(BaseModel):
    """One matching event with the evidence needed to trace it back to the original log."""

    event_id: uuid.UUID
    source_id: str
    occurred_at: datetime | None
    level: str | None
    locator: dict[str, Any] = Field(description="Where the original lives: file path, byte offsets, lines and sha256")
    raw_text: str = Field(description="Redacted original event text")
    score: float = Field(description="Relevance after merging semantic and keyword ranking; higher is better")
    matched_by: list[Literal["semantic", "keyword"]]


class SourceCoverage(BaseModel):
    """How complete the index is for a searched source, so 'no results' is never mistaken for 'no logs'."""

    source_id: str
    status: Literal["complete", "stale", "partial", "not_indexed", "error"]
    indexed_through: datetime | None
    last_error: str | None = None


class SearchLogsResponse(BaseModel):
    results: list[SearchHit]
    coverage: list[SourceCoverage]
