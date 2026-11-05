"""Removes secrets and personal data from log text BEFORE it is parsed, stored, embedded or searched.

Pure and bounded-time: patterns are precompiled and every value scan is capped, because log content is untrusted input
(a hostile line must not stall the indexer or smuggle a secret past the rules). Replacements keep the surrounding
structure so JSON and key=value text stay parseable: a secret that was not a JSON string (number, null, array, object)
becomes a string mask. Redaction is idempotent: redacting redacted text changes nothing.

The text is first NFKC-normalised and stripped of invisible format characters (zero-width, bidi), so look-alike
spacing cannot hide a key; the redacted text is therefore the normalised form of the input.

Known trade-offs: the key/value rule matches by name, so harmless values such as `max_tokens=100` are redacted too
(over-redacting costs a little recall, under-redacting leaks). Secrets stated in prose ("the password is x"), split
across assembler event boundaries, or hidden behind homoglyph letters are out of reach of pattern matching.
"""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from app.ingestion.pseudonym import email_code

MAX_VALUE_SPAN = 4096  # the longest value one key/value rule will mask in one go; also bounds the work per key
CARD_MIN_DIGITS, CARD_MAX_DIGITS = 14, 19

_FORMAT_CHARS = re.compile("[­​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_ALREADY_MASKED = re.compile(r"""(?:\\?["'])?\[REDACTED:[a-z_]+(?::u[0-9a-f]{11})?\](?:\\?["'])?""")
_SECRET_NAMES = (
    r"(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    kinds: frozenset[str]  # which kinds of secret were found, for logging; never the secrets themselves


def _mask(kind: str) -> str:
    return f"[REDACTED:{kind}]"


# --- key/value rules: "password=x", "Authorization: Bearer x", {"token": 123}, ... -----------------------------------


@dataclass(frozen=True)
class _PairRule:
    kind: str
    key: re.Pattern[str]  # matches the key through its separator; group "q" is the key's closing quote, if any
    bare_value_ends_at: str  # characters that end an unquoted value


_PAIR_RULES = (
    _PairRule("auth", re.compile(
        r"""(?i)\b(?:proxy-)?authorization(?P<q>\\?["'])?[ \t]*[:=][ \t]*"""), "\r\n})]"),  # the scheme + credentials
    _PairRule("cookie", re.compile(r"""(?i)\b(?:set-)?cookie(?P<q>\\?["'])?[ \t]*[:=][ \t]*"""), "\r\n})]"),
    _PairRule("secret", re.compile(
        rf"""(?i){_SECRET_NAMES}\w{{0,40}}+(?P<q>\\?["'])?[ \t]*[:=][ \t]*"""), " \t\r\n,;&\"'})]"),
)


def _closing_bracket(text: str, start: int, limit: int) -> int | None:
    """Index just past the bracket that closes the one at `start`, string-aware, or None if not closed in range."""
    depth, quote, i = 0, "", start
    while i < limit:
        char = text[i]
        if quote:
            if char == "\\":
                i += 1
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _bare_end(text: str, pos: int, limit: int, stops: str) -> int:
    i = pos
    while i < limit:
        if text.startswith("[REDACTED:", i):  # an existing mask is one unit, not a delimiter
            closing = text.find("]", i, limit)
            i = closing + 1 if closing != -1 else i + 1
        elif text[i] in stops:
            break
        else:
            i += 1
    return i


def _value_span(text: str, pos: int, stops: str) -> tuple[int, str]:
    """End of the value that starts at `pos`, and the quote that wrapped it ("" when it was not a string)."""
    limit = min(len(text), pos + MAX_VALUE_SPAN)
    if text.startswith(('\\"', "\\'"), pos):  # JSON inside a JSON string: the value is \"...\"
        quote = text[pos:pos + 2]
        end = text.find(quote, pos + 2, limit)
        return (end + 2 if end != -1 else _bare_end(text, pos, limit, "\r\n")), quote
    char = text[pos:pos + 1]
    if char in ("\"", "'"):
        i = pos + 1
        while i < limit:
            if text[i] == "\\":
                i += 1
            elif text[i] == char:
                return i + 1, char
            elif text[i] == "\n":
                break  # unterminated: ends with the line
            i += 1
        return min(i, limit), char
    if char in ("[", "{"):
        end = _closing_bracket(text, pos, limit)
        if end is not None:
            return _bare_end(text, end, limit, stops), ""  # also takes anything glued to the closing bracket
        newline = text.find("\n", pos, limit)  # never closed: mask to the end of the line (or the span cap) so the
        return (newline if newline != -1 else limit), ""  # scan is not repeated for every key that follows
    return _bare_end(text, pos, limit, stops), ""


def _redact_pairs(text: str, rule: _PairRule, kinds: set[str]) -> str:
    out: list[str] = []
    cursor = pos = 0
    while match := rule.key.search(text, pos):
        start = match.end()
        end, quote = _value_span(text, start, rule.bare_value_ends_at)
        pos = max(end, start)
        if end <= start or _ALREADY_MASKED.fullmatch(text[start:end]):
            continue
        wrap = quote or ('"' if match.group("q") else "")  # inside JSON a non-string secret must become a string
        out += (text[cursor:start], wrap, _mask(rule.kind), wrap)
        cursor = end
        kinds.add(rule.kind)
    out.append(text[cursor:])
    return "".join(out)


# --- card numbers ---------------------------------------------------------------------------------------------------

_DIGIT_GROUP = re.compile(r"(?<![A-Za-z0-9])\d{4,19}(?![A-Za-z0-9])")  # card groups are 4+ digits (4-4-4-4, 4-6-5)


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


@lru_cache(maxsize=8192)
def _is_card(digits: str) -> bool:
    # Real card prefixes only (3, 4, 5, 6, or MasterCard 2221-2720): epoch milliseconds and compact timestamps such as
    # 20261029081523 start with 1 or 20xx, so they are not mistaken for cards even when the checksum happens to pass.
    prefix_ok = digits[0] in "3456" or 2221 <= int(digits[:4]) <= 2720
    return CARD_MIN_DIGITS <= len(digits) <= CARD_MAX_DIGITS and prefix_ok and _luhn_valid(digits)


def _redact_cards(text: str, kinds: set[str]) -> str:
    """Masks card numbers written as one run of digits or as groups joined by a single space or dash, wherever in a
    line they sit ("2026-10-29 4111111111111111" is found); the groups of a date or a timestamp never combine."""
    groups = [(m.start(), m.end()) for m in _DIGIT_GROUP.finditer(text)]
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(groups):
        digits, longest, j = "", None, i
        while j < len(groups):
            if j > i and not (groups[j - 1][1] + 1 == groups[j][0] and text[groups[j - 1][1]] in " -"):
                break
            digits += text[groups[j][0]:groups[j][1]]
            if len(digits) > CARD_MAX_DIGITS:
                break
            if _is_card(digits):
                longest = j
            j += 1
        if longest is None:
            i += 1
        else:
            spans.append((groups[i][0], groups[longest][1]))
            i = longest + 1
    if not spans:
        return text
    kinds.add("card")
    out, cursor = [], 0
    for start, end in spans:
        out += (text[cursor:start], _mask("card"))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


# --- single-pattern rules -------------------------------------------------------------------------------------------

_PEM_BODY = r"(?:\s*(?:[A-Za-z0-9+/=]{16,}|(?:Proc-Type|DEK-Info|Version|Comment):[^\n]*|=[A-Za-z0-9+/]{4}\b))*"
_PEM = (
    r"-----BEGIN [A-Z0-9 ]{0,30}?PRIVATE KEY(?: BLOCK)?-----" + _PEM_BODY
    + r"(?:\s*-----END [A-Z0-9 ]{0,30}?PRIVATE KEY(?: BLOCK)?-----)?"
)


def _just(kind: str) -> Callable[[re.Match[str]], str]:
    return lambda match: _mask(kind)


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}")
_BEFORE_PAIRS = (("private_key", re.compile(_PEM), _just("private_key")),)
_AFTER_PAIRS = (
    ("jwt", re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]*"), _just("jwt")),
    ("aws_key", re.compile(r"(?i)\b(?:AKIA|ASIA|AIDA|AGPA|AROA)[0-9A-Z]{16}\b"), _just("aws_key")),
    ("api_key", re.compile(
        r"(?i)\b(?:sk-|gsk_|ghp_|gho_|ghu_|ghs_|github_pat_|glpat-|xox[abprs]-)[A-Za-z0-9_-]{16,}"), _just("api_key")),
    # userinfo runs to the LAST "@" before the path, so a password containing "@" is masked whole; before emails
    ("credentials", re.compile(r"(://)[^/\s:@]{1,256}:[^/\s]{1,256}@"), lambda m: m.group(1) + _mask("credentials") + "@"),
    ("email", _EMAIL, _just("email")),
)


def _after_pairs(pseudonym_key: bytes | None):
    """The rules after the key/value pass. With a key the e-mail mask carries a stable code for that address."""
    if pseudonym_key is None:
        return _AFTER_PAIRS
    coded = lambda match: f"[REDACTED:email:{email_code(pseudonym_key, match.group(0))}]"  # noqa: E731
    return (*_AFTER_PAIRS[:-1], ("email", _EMAIL, coded))


def _apply(text: str, rules, kinds: set[str]) -> str:
    for kind, pattern, replace in rules:
        text, count = pattern.subn(replace, text)
        if count:
            kinds.add(kind)
    return text


def redact(text: str, *, pseudonym_key: bytes | None = None) -> RedactionResult:
    """Without a key every e-mail becomes `[REDACTED:email]`; with one, `[REDACTED:email:u<11 hex>]`, the same code for
    the same address (app.ingestion.pseudonym)."""
    kinds: set[str] = set()
    text = unicodedata.normalize("NFKC", _FORMAT_CHARS.sub("", text))
    text = _apply(text, _BEFORE_PAIRS, kinds)
    for rule in _PAIR_RULES:
        text = _redact_pairs(text, rule, kinds)
    text = _apply(text, _after_pairs(pseudonym_key), kinds)
    return RedactionResult(text=_redact_cards(text, kinds), kinds=frozenset(kinds))
