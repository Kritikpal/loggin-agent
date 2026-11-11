from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

FileStatus = Literal["not_indexed", "error", "stale", "partial", "complete", "missing", "skipped"]


class ProjectSummary(BaseModel):
    id: str
    title: str
    type: str
    status: Literal["ok", "invalid"]
    file_count: int
    indexed_from: datetime | None
    indexed_to: datetime | None
    error: str | None


class ProjectDetail(ProjectSummary):
    description: str
    timezone: str
    include: str


class ProjectFile(BaseModel):
    """One log file of a project. `id` is the file's short id (its stem); `last_error` also carries a skip reason."""

    id: str
    name: str
    size_bytes: int | None
    modified_at: datetime | None
    status: FileStatus
    event_count: int
    first_at: datetime | None
    last_at: datetime | None
    parsed_ratio: float | None
    last_error: str | None


class ProjectCreate(BaseModel):
    """A new project. `id` is the slug used in URLs and as the prefix of its log source ids."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    type: str
    description: str = ""
    timezone: str = "UTC"
    event_start: str | None = None  # regex: a line matching it starts a new event (default: the type's preset)
    app_packages: list[str] | None = None
    field_mappings: dict[str, str] | None = None


class ProjectUpdate(BaseModel):
    """Only the fields sent change; send null for event_start, app_packages or field_mappings to reset it."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    type: str | None = None
    description: str | None = None
    timezone: str | None = None
    event_start: str | None = None
    app_packages: list[str] | None = None
    field_mappings: dict[str, str] | None = None
