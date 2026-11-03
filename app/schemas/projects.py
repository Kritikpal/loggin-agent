from datetime import datetime
from typing import Literal

from pydantic import BaseModel

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
