"""What every source reader (local files only) hands to the indexing pipeline."""

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

from app.ingestion.assembler import RawEvent

logger = logging.getLogger(__name__)

ReadStatus = Literal["ok", "rotated", "missing", "error"]
MAX_HEAD_BYTES = 256  # how much of the consumed start of a file is fingerprinted to notice replacement


@dataclass(frozen=True)
class Cursor:
    """Where the next read of a file starts. Stored as JSON text in source_index_state.cursor.

    `offset` is always the start of the first event NOT yet handed over, so an event that is still being written is
    never split across two batches. `head_*` fingerprint the first min(256, offset) bytes: they stay the same while
    the file only grows, and change when the file is replaced or rewritten."""

    offset: int = 0
    line: int = 1  # 1-based line number at `offset`
    head_len: int = 0  # always min(MAX_HEAD_BYTES, offset)
    head_sha: str = ""
    file_id: str = ""  # device:inode (empty where the platform has none)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str | None) -> "Cursor":
        """A missing or damaged cursor means 'start from the beginning': re-indexing is idempotent, losing data is not.
        The cursor comes from the database, so every field is checked before it is used to seek or allocate."""
        if not text:
            return cls()
        try:
            data = json.loads(text)
            offset, line, head_len = data["offset"], data["line"], data["head_len"]
            head_sha, file_id = data["head_sha"], data["file_id"]
            numbers_ok = all(type(n) is int and n >= 0 for n in (offset, line, head_len)) and line >= 1
            if numbers_ok and head_len == min(MAX_HEAD_BYTES, offset) and isinstance(head_sha, str) and isinstance(file_id, str):
                return cls(offset, line, head_len, head_sha, file_id)
        except (ValueError, TypeError, KeyError):
            pass
        logger.warning("Ignoring an unreadable source cursor; indexing starts from the beginning")
        return cls()


@dataclass(frozen=True)
class SourceEvent:
    """One assembled event plus the evidence needed to find it again in the original log."""

    event: RawEvent
    event_key: str  # stable within a source: the same bytes at the same place always give the same key
    locator: Mapping[str, Any]


@dataclass(frozen=True)
class ReadResult:
    events: tuple[SourceEvent, ...]
    next_cursor: Cursor
    status: ReadStatus = "ok"  # rotated: the file was replaced or truncated, so reading restarted from the beginning
    has_more: bool = False  # call again right away: more complete data is waiting
    error: str | None = None  # an exception class name, never log content
    observed_size: int | None = None


class SourceReader(Protocol):
    def read_batch(self, cursor: Cursor | None = None, max_events: int = 1000) -> ReadResult: ...
