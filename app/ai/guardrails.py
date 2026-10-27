"""Deterministic guardrails for the AI chat: pure checks, no model calls. See
docs/superpowers/specs/2026-10-27-guardrails-design.md for where each one runs.

Each check returns the check's name when it trips, else None. A trip is logged with trip(); the user only ever sees
BLOCKED_TEXT or WITHHELD_TEXT, never which check fired, so the patterns cannot be probed.
"""

import logging
import re
import unicodedata
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

logger = logging.getLogger(__name__)

BLOCKED_TEXT = "I can't help with that request. Nothing was changed."
WITHHELD_TEXT = "This data was withheld by a safety check."

# ponytail: keyword patterns, a reworded attack gets past them. Grow them from tests/test_guardrails.py; add the LLM
# classifier (spec approach B) together with authorization when the APIs open to more users.
INJECTION_PATTERNS = (
    # "my" is left out on purpose: the manager may retract their own request ("forget my previous instructions")
    r"\b(?:ignore|disregard|forget)(?:\s+(?:all|any|the|your))?(?:\s+(?:previous|prior|above|earlier))?"
    r"\s+(?:instructions?|rules?|prompts?)\b",
    r"\byou are now\b",
    r"\byour new instructions\b|^\s*new instructions\s*:",
    r"\bsystem prompt\b",
    r"\bdeveloper mode\b",
    r"\bjailbreak",
    r"</?\s*(?:system|untrusted_\w+)\s*>",
    r"\[/?inst\]",
    r"^\s*#+\s*system\b",
    r"^\s*(?:system|assistant)\s*:",
)
_INJECTION = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS), re.MULTILINE)
# zero-width, text-direction overrides and the invisible tag characters: normal business text never has them
_HIDDEN = re.compile(r"[\u200b-\u200f\u2060\ufeff\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]")

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
MIN_PHONE_DIGITS = 10
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_AMOUNT = re.compile(r"\d+\.\d{1,2}(?!\d)")  # 45230.00, 12.4, 20.10: money, percentages, dotted dates, never phones
_LONG_DIGIT_GROUP = re.compile(r"\d{4,}")  # real phone numbers have one; a list of sizes "28 30 32 34 36" does not

_ACTION_CLAIM = re.compile(
    r"\bi(?:['’]ve| have)? (?:created|launched|sent|changed|updated|applied|scheduled|deleted)\b"
)
_LINK = re.compile(r"https?://|\bwww\.|\b[\w-]+\.(?:com|net|org|in|io|co|shop|store)\b")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
BANNED_CLAIMS = ("guarantee", "risk-free", "free", "best price", "lowest price", "100%")


def normalize(text: str) -> str:
    """NFKC (folds look-alike letters), casefold, and runs of spaces/tabs collapsed. Line breaks stay, so a role tag
    at the start of a line is still at the start of a line."""
    return re.sub(r"[ \t]+", " ", unicodedata.normalize("NFKC", text).casefold())


def injection(text: str) -> str | None:
    if _HIDDEN.search(text) or _INJECTION.search(normalize(text)):
        return "injection"
    return None


def contact_details(text: str) -> str | None:
    if EMAIL.search(text):
        return "contact_details"
    for match in PHONE.finditer(_AMOUNT.sub(" ", _ISO_DATE.sub(" ", text))):
        phone = match.group()
        digits = sum(ch.isdigit() for ch in phone)
        if "\n" not in phone and digits >= MIN_PHONE_DIGITS and _LONG_DIGIT_GROUP.search(phone):
            return "contact_details"
    return None


def action_claim(text: str) -> str | None:
    return "action_claim" if _ACTION_CLAIM.search(normalize(text)) else None


def _numbers(text: str) -> list[Decimal]:
    return [Decimal(n.replace(",", "")) for n in NUMBER.findall(text)]


def ungrounded_numbers(reply: str, sources: Iterable[str]) -> list[str]:
    """Numbers in `reply` (as written) that no source supports; empty means the reply is grounded.

    A reply number is supported when some source number rounds to it at the reply's precision ("1,235" for 1234.56),
    or does so in thousands when the reply writes a "k" ("1.2k"). NUMBER never captures a sign, so a tool's -12.50
    supports "fell 12.5%". Numbers the model worked out itself are not supported: the tools must return them.
    """
    known = [n for source in sources for n in _numbers(source)]
    unsupported = []
    for match in NUMBER.finditer(reply):
        written = match.group()
        value = Decimal(written.replace(",", ""))
        step = Decimal(1).scaleb(value.as_tuple().exponent)  # 1 for "1235", 0.1 for "12.5"
        thousands = reply[match.end():match.end() + 1].lower() == "k"
        if not any(_rounds_to(n / 1000 if thousands else n, step, value) for n in known):
            unsupported.append(written)
    return unsupported


def _rounds_to(source: Decimal, step: Decimal, value: Decimal) -> bool:
    try:
        return source.quantize(step, ROUND_HALF_UP) == value
    except InvalidOperation:  # more digits than Decimal's precision (an id, not an amount): cannot support anything
        return False


def campaign_text(name: str, message: str, discount_percent: Decimal) -> list[str]:
    """Reasons the customer-facing campaign text may not be proposed; empty when it is clean."""
    reasons = []
    if injection(name) or injection(message):
        reasons.append("campaign: contains instructions")
    if _LINK.search(normalize(f"{name}\n{message}")):
        reasons.append("message: contains a link")
    if contact_details(message):
        reasons.append("message: contains contact details")
    text = normalize(message)
    for claim in BANNED_CLAIMS:
        if re.search(rf"(?<!\w){re.escape(claim)}(?:d|s)?(?!\w)", text):
            reasons.append(f"message: makes a banned claim ('{claim}')")
    for percent in _PERCENT.findall(text):
        if Decimal(percent) != discount_percent:
            reasons.append(f"message: discount {percent}% does not match the proposal's {discount_percent}%")
    return reasons


def trip(point: str, check: str, text: str, conversation: object = None, **extra: str) -> None:
    """One log line per block, so false positives can be reviewed and the patterns tuned."""
    tail = "".join(f" {k}={v}" for k, v in extra.items())
    logger.warning(
        "guardrail_trip point=%s check=%s conversation=%s sample=%r%s",
        point, check, conversation if conversation is not None else "-", text[:80], tail,
    )
