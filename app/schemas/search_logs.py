import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TimeAndLevelFilters(BaseModel):
    """The time and level filters with their rules, shared by the API's filters and the agent's (which name files,
    not source ids)."""

    model_config = ConfigDict(extra="forbid")

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


class LogFilters(TimeAndLevelFilters):
    """The filters of the search API and the search_logs tool."""

    source_ids: list[str] | None = Field(default=None, description="Restrict to these approved log sources")


class SearchLogsRequest(LogFilters):
    """Step 2 contract of the search_logs tool."""

    query: str = Field(min_length=1, max_length=2000, description="Natural-language search query")
    top_k: int = Field(default=10, ge=1, le=50, description="Maximum number of results")


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
    signature: str | None = Field(default=None, description="Id of the event's kind; the same for every event of it")


class SourceCoverage(BaseModel):
    """How complete the index is for a searched source, so 'no results' is never mistaken for 'no logs'."""

    source_id: str
    status: Literal["complete", "stale", "partial", "not_indexed", "error"]
    indexed_through: datetime | None
    last_error: str | None = None


class SearchLogsResponse(BaseModel):
    results: list[SearchHit]
    coverage: list[SourceCoverage]
