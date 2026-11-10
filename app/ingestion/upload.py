"""A whole log file sent in one request: split into events like a file on disk would be, then stored through the
same steps as pushed events (redact, parse, dedup by content). The file itself is never kept anywhere."""

from dataclasses import replace

from app.core.errors import InvalidPayloadError
from app.ingestion.assembler import assemble, start_pattern_for
from app.ingestion.pipeline import prepare_event
from app.ingestion.push import push_event_key
from app.ingestion.readers.base import SourceEvent
from app.ingestion.sources import PushSource
from app.ingestion.store import EventRow

LOCATOR = {"kind": "upload"}
UTF8_BOM = b"\xef\xbb\xbf"


def build_upload_rows(raw: bytes, source: PushSource) -> list[EventRow]:
    """One row per event of the file. CPU-bound (redaction, parsing): call it from a worker thread. The body size is
    capped by the caller, which bounds the work.
    ponytail: the key hashes the redacted event text, so identical timestamp-less events collapse into one; a
    timestamp in the text (every preset log has one) keeps real repeats apart."""
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidPayloadError("The body must be UTF-8 text.") from None
    if b"\x00" in raw:
        raise InvalidPayloadError("The body must be UTF-8 text.")
    data = raw.removeprefix(UTF8_BOM)
    pieces = data.split(b"\n")  # only "\n" ends a line, so a lone CR stays inside it
    lines, offset = [], 0
    for i, piece in enumerate(pieces):
        line = piece + b"\n" if i < len(pieces) - 1 else piece
        if line:
            lines.append((offset, line))
            offset += len(line)
    pattern = start_pattern_for(source.format, source.event_start)
    rows = []
    for event in assemble(lines, pattern):
        if not event.text.strip():
            continue
        row = prepare_event(SourceEvent(event, "", LOCATOR), source)
        rows.append(replace(row, event_key=push_event_key(row.raw_text, None)))  # hash the redacted text only
    return rows
