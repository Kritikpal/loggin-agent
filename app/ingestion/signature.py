"""Event signatures: one short id per kind of event, so tools can count and list kinds instead of sampling text.

A signature is computed once at index time from the parsed level, logger and message. The variable parts (ids,
addresses, numbers, quoted values, masked user names) become <*>, and the text stops where a second exception or a
"[Root exception" tail begins, since those details vary between events of one kind. Pure and deterministic: the same
event always gets the same signature, which is what lets the model reuse a sig it saw earlier. Changing these rules
changes signatures, so it needs a re-index.
"""

import hashlib
import re
from dataclasses import dataclass

from app.ingestion.fields.resolver import resolve
from app.ingestion.sources import SourceBase

MAX_TEMPLATE = 200
SIG_LENGTH = 8  # hex characters; thousands of kinds per project stay far from collisions
PLACEHOLDER = "<*>"

_DAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_VARIABLE = (  # applied in order: the specific shapes before the plain numbers they contain
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),  # UUID
    re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),  # e-mail
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"),  # IPv4 with optional port
    re.compile(rf"\b{_DAY} {_MONTH} +\d{{1,2}} \d{{1,2}}:\d{{2}}:\d{{2}}(?: [A-Z]{{2,5}})? \d{{4}}\b"),  # Fri May 29 19:29:22 IST 2026
    re.compile(r"'[^'\n]*'|\"[^\"\n]*\""),  # quoted values
    re.compile(r"\b0x[0-9a-f]+\b|\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", re.I),  # hex ids and bundle hashes (8a878677)
    re.compile(r"\w\*{2,}\w"),  # user names WSO2 masked itself (C*****k): the star count varies
    re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|sec|secs|min|h|b|kb|mb|gb)\b", re.I),  # 172288623ms, 30s, 512KB
    re.compile(r"\b\d+(?:[.,:]\d+)*\b"),  # numbers, times, durations
)
_EXCEPTION = re.compile(r"\b(?:[A-Za-z_$][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error)\b")
_ROOT_TAIL = re.compile(r"\s*\[Root exception", re.I)
_QUALIFIED = re.compile(r"\b(?:[a-z_$][\w$]*\.)+([A-Z][\w$]*)")  # org.wso2.x.UserStoreException -> UserStoreException
_REPEATED = re.compile(rf"(?:{re.escape(PLACEHOLDER)}[\s,;:]*){{2,}}")


@dataclass(frozen=True)
class Signature:
    sig: str
    template: str


def event_signature(*, level: str | None, logger: str | None, message: str | None, raw_text: str) -> Signature:
    body = message if message else (raw_text.splitlines() or [""])[0]
    template = _template(level, logger, body)
    return Signature(hashlib.sha1(template.encode("utf-8")).hexdigest()[:SIG_LENGTH], template)


def signature_for(text: str, source: SourceBase) -> Signature:
    """The signature of a stored (already redacted) event: parsed as at index time, so a backfill matches indexing."""
    try:
        parsed = resolve(text, source).parsed
    except Exception:  # resolve never raises by contract; an unparsed event still gets a signature
        return event_signature(level=None, logger=None, message=None, raw_text=text)
    return event_signature(level=parsed.level, logger=parsed.logger, message=parsed.message, raw_text=text)


def _template(level: str | None, logger: str | None, body: str) -> str:
    head = f"{level or '-'} {{{logger.rsplit('.', 1)[-1]}}} -" if logger else (level or "-")
    text = _cut(body)
    for pattern in _VARIABLE:
        text = pattern.sub(PLACEHOLDER, text)
    text = _QUALIFIED.sub(r"\1", text)
    text = _REPEATED.sub(PLACEHOLDER + " ", text)
    text = " ".join(text.split())
    return f"{head} {text}"[:MAX_TEMPLATE].rstrip()


def _cut(text: str) -> str:
    """Keep the first exception and its message; drop a second exception or a "[Root exception" tail."""
    if root := _ROOT_TAIL.search(text):
        text = text[:root.start()]
    exceptions = list(_EXCEPTION.finditer(text))
    return text[:exceptions[1].start()] if len(exceptions) > 1 else text
