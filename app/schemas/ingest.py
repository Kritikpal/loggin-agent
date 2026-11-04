from pydantic import BaseModel, field_validator

from app.ingestion.sanitize import sanitize


class PushEvent(BaseModel):
    """One pushed log event. Keys other than these are ignored."""

    message: str
    timestamp: str | None = None  # ISO 8601; a naive time is read in the project's timezone
    level: str | None = None

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not sanitize(value).strip():  # what is left once control characters are removed is what gets stored
            raise ValueError("message must not be blank")
        return value


class IngestResponse(BaseModel):
    received: int
    stored: int
    duplicates: int
