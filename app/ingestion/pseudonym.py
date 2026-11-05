"""Stable pseudonyms for e-mail addresses, so redaction can hide who someone is without hiding that it is the same person.

The code is `u` plus the first 11 hex characters of HMAC-SHA256(key, lower-cased address). Same address, same code; the
address cannot be read back without the key. The letter matters: the card-number rule looks at runs of digits, and a code
that happened to be all digits could otherwise be rewritten as a card mask. The key is a secret: never log it or an address.
"""

import hashlib
import hmac

from app.core.config import get_settings

CODE_HEX_CHARS = 11


def email_code(key: bytes, address: str) -> str:
    digest = hmac.new(key, address.lower().encode("utf-8"), hashlib.sha256).hexdigest()
    return f"u{digest[:CODE_HEX_CHARS]}"


def configured_pseudonym_key() -> bytes | None:
    """The PSEUDONYM_KEY setting as bytes, or None when it is unset or blank (e-mails then stay plainly masked)."""
    key = (get_settings().pseudonym_key or "").strip()
    return key.encode("utf-8") if key else None
