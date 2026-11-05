from pydantic import BaseModel


class PushEvent(BaseModel):
    """One pushed log event. Keys other than these are ignored. A blank `message` is skipped, not rejected."""

    message: str
    timestamp: str | None = None  # ISO 8601; a naive time is read in the project's timezone
    level: str | None = None


class IngestResponse(BaseModel):
    received: int  # every event in the request: stored + duplicates + skipped
    stored: int
    duplicates: int
    skipped: int  # blank messages, which are never stored
