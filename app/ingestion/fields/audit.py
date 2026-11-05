"""The facts of a WSO2 AUDIT_LOG message as fields, so they can be counted instead of guessed from sampled lines.

Two shapes carry the same facts: `Initiator : x | Action : y | Target : z | Data : { ... } | Result : r` and
`Initiator=x Action=y Target=z Data={...} Outcome=r`. Each fact has its own bounded pattern on the (already redacted)
message instead of a JSON parse, so both shapes need one code path, nested `Claims` cannot confuse it, and a half-formed
line still yields what it has. Only the keys below are kept: never `Claims` (roles and groups: personal data, and large).
Pure and deterministic; every pattern is linear-time and sees at most SCAN_CHARS.
"""

import re

SCAN_CHARS = 16 * 1024
MAX_VALUE_CHARS = 256

# A header value is a whole redaction mask (it holds ':' but no spaces) or a run up to whitespace or the '|' separator.
_HEADER_VALUE = r"(\[REDACTED:[^\]\n]{1,64}\]|[^\s|]{1,256})"
_ACTION = re.compile(r"\bAction\s*[:=]\s*([A-Za-z][A-Za-z-]{0,63})")
_RESULT = re.compile(r"\b(?:Result|Outcome)\s*[:=]\s*([A-Za-z]{1,32})")
_ACTOR = re.compile(rf"\bInitiator\s*[:=]\s*{_HEADER_VALUE}")
_TARGET = re.compile(rf"\bTarget\s*[:=]\s*{_HEADER_VALUE}")
_USER = re.compile(r'"AuthenticatedUser"\s*:\s*"([^"\\\n]{1,256})"')
_APPLICATION = re.compile(r'"ServiceProviderName"\s*:\s*"([^"\\\n]{1,256})"')
_STORE_DOMAIN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}/(?=.)")  # "PRIMARY/admin" -> "admin"


def audit_fields(message: str) -> dict[str, str]:
    """The `audit_*` fields found in the message; a key is left out when its value is missing. Never raises."""
    try:
        text = message[:SCAN_CHARS]
        results = list(_RESULT.finditer(text))  # the verdict is the last one: the Data block may mention the word too
        values = {
            "audit_action": _first(_ACTION, text),
            "audit_result": results[-1][1] if results else None,
            "audit_actor": _without_domain(_first(_ACTOR, text)),
            "audit_target": _first(_TARGET, text),
            "audit_user": _without_domain(_first(_USER, text)),
            "audit_application": _first(_APPLICATION, text),
        }
        return {key: value[:MAX_VALUE_CHARS] for key, value in values.items() if value}
    except Exception:  # untrusted text must never cost the event
        return {}


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match[1] if match else None


def _without_domain(value: str | None) -> str | None:
    return _STORE_DOMAIN.sub("", value, count=1) if value else value
