"""Reads a local log file incrementally and read-only.

The file is opened "rb" and never written or executed. Each call resumes from a Cursor, hands over complete events
only, and returns the cursor to use next time. The newest event is held back until a later start line shows it is
finished (or the file has been quiet for `settle_seconds` and the read reached its end), so a stack trace still being
written is never split.

Known limits, all deliberate:
- An event or line longer than `max_read_bytes` (16 MB) is split or clipped; keep the buffer well above any event.
- A rewrite that keeps the first 256 bytes of the consumed part identical is not seen as a replacement.
- Quiet is judged by the file's mtime against this machine's clock, so a badly skewed clock delays the last event.
- On Windows the handle is open without FILE_SHARE_DELETE, so a logger renaming the file during the (millisecond)
  read can fail; the next poll picks the new file up.
"""

import hashlib
import os
import time
from collections.abc import Callable
from typing import BinaryIO

from app.ingestion.assembler import RawEvent, assemble, start_pattern_for
from app.ingestion.readers.base import MAX_HEAD_BYTES, Cursor, ReadResult, SourceEvent
from app.ingestion.sources import FileSource

MAX_READ_BYTES = 16 * 1024 * 1024  # bounds memory per batch
UTF8_BOM = b"\xef\xbb\xbf"
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


class FileReader:
    def __init__(
        self,
        source: FileSource,
        settle_seconds: float = 5.0,
        max_read_bytes: int = MAX_READ_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._settle_seconds = settle_seconds
        self._max_read_bytes = max_read_bytes
        self._clock = clock
        self._start_pattern = start_pattern_for(source.format, source.event_start)

    def read_batch(self, cursor: Cursor | None = None, max_events: int = 1000) -> ReadResult:
        cursor = cursor or Cursor()
        try:
            with self._source.path.open("rb") as handle:
                return self._read(handle, cursor, max(1, max_events))
        except FileNotFoundError:
            return ReadResult((), cursor, status="missing", error="FileNotFoundError")  # old events stay indexed
        except OSError as exc:
            return ReadResult((), cursor, status="error", error=type(exc).__name__)

    def _read(self, handle: BinaryIO, cursor: Cursor, max_events: int) -> ReadResult:
        stat = os.fstat(handle.fileno())
        file_id = f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else ""
        status = "ok"
        replaced, prefix = self._check_replaced(handle, cursor, stat.st_size, file_id)
        if replaced:
            cursor, status, prefix = Cursor(), "rotated", b""
        handle.seek(cursor.offset)
        data = handle.read(self._max_read_bytes)
        if cursor.offset == 0 and data.startswith(UTF16_BOMS):
            return ReadResult((), cursor, status="error", error="UnsupportedEncoding", observed_size=stat.st_size)

        quiet = self._clock() - stat.st_mtime >= self._settle_seconds
        at_eof = cursor.offset + len(data) >= stat.st_size
        full = len(data) >= self._max_read_bytes
        complete_end = data.rfind(b"\n") + 1
        giant_line = full and complete_end == 0  # one line longer than the buffer: taken as it is, clipped later
        # Never take a half-written line, unless the file is quiet and this is its end (or the line is a giant).
        consumed = data if giant_line or (quiet and at_eof) else data[:complete_end]
        lines = _split_lines(consumed, cursor.offset)
        events = list(assemble(lines, self._start_pattern, first_line=cursor.line))
        final = giant_line or (quiet and at_eof)  # nothing more can extend the newest event
        ready, held = (events, None) if final else (events[:-1], events[-1] if events else None)
        if held is not None and full and not ready and held.start_offset == cursor.offset:
            ready, held = events, None  # a single event fills the whole buffer: emit it, or reading would never advance

        batch = ready[:max_events]
        next_event = ready[max_events] if len(ready) > max_events else held
        if next_event is not None:
            next_offset, next_line = next_event.start_offset, next_event.start_line
        elif ready or final or full:  # `full`: a buffer of blank lines still has to be stepped over
            next_offset, next_line = cursor.offset + len(consumed), cursor.line + consumed.count(b"\n")
        else:
            next_offset, next_line = cursor.offset, cursor.line

        return ReadResult(
            events=tuple(self._source_event(e, data, cursor.offset) for e in batch),
            next_cursor=_cursor_at(prefix, data, cursor.offset, next_offset, next_line, file_id),
            status=status,
            has_more=len(ready) > max_events or stat.st_size > cursor.offset + len(data),
            observed_size=stat.st_size,
        )

    @staticmethod
    def _check_replaced(handle: BinaryIO, cursor: Cursor, size: int, file_id: str) -> tuple[bool, bytes]:
        """(True, b"") when the bytes already consumed are no longer the start of this file, else (False, those bytes'
        fingerprinted prefix). Cursors are validated on load, so head_len is at most 256 here."""
        if cursor.offset == 0:
            return False, b""
        if size < cursor.offset or (cursor.file_id and file_id and cursor.file_id != file_id):
            return True, b""
        handle.seek(0)
        head = handle.read(cursor.head_len)
        if len(head) < cursor.head_len or (cursor.head_sha and _sha(head) != cursor.head_sha):
            return True, b""
        return False, head

    def _source_event(self, event: RawEvent, data: bytes, base: int) -> SourceEvent:
        digest = hashlib.sha256(data[event.start_offset - base:event.end_offset - base]).hexdigest()
        locator = {
            "type": "file", "path": str(self._source.path), "offset_start": event.start_offset,
            "offset_end": event.end_offset, "line_start": event.start_line, "line_end": event.end_line,
            "sha256": digest,
        }
        # the hash makes the key safe across rotation: a new file's bytes at an old offset cannot overwrite old evidence
        return SourceEvent(event, f"{event.start_offset}:{digest[:16]}", locator)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cursor_at(prefix: bytes, data: bytes, base: int, offset: int, line: int, file_id: str) -> Cursor:
    """The cursor for `offset`. Its fingerprint comes from bytes this very read already holds (the verified prefix
    plus the chunk), never from a second look at a file that may have changed in the meantime."""
    head_len = min(MAX_HEAD_BYTES, offset)
    known = prefix if base > MAX_HEAD_BYTES else prefix + data  # bytes [0, base + len(data)) when base <= 256
    return Cursor(offset, line, head_len, _sha(known[:head_len]) if head_len else "", file_id)


def _split_lines(data: bytes, base: int) -> list[tuple[int, bytes]]:
    """(absolute byte offset, line including its "\\n") pairs. Only "\\n" ends a line, so a lone CR stays inside it."""
    pieces = data.split(b"\n")
    lines = [piece + b"\n" for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])  # a last line without a terminator (only taken once the file is quiet)
    out, offset = [], base
    for line in lines:
        out.append((offset, line))
        offset += len(line)
    if base == 0 and out and out[0][1].startswith(UTF8_BOM):
        out[0] = (len(UTF8_BOM), out[0][1][len(UTF8_BOM):])  # the BOM is not part of the first event
    return out
