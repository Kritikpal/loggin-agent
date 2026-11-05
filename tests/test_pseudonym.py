"""Stable e-mail pseudonyms: the same address gets the same code, and without a key nothing changes."""

import re

import pytest

from app.core.config import Settings
from app.ingestion import pseudonym
from app.ingestion.pseudonym import configured_pseudonym_key, email_code
from app.ingestion.redaction import redact

KEY = b"test-key"
CODED_MASK = r"\[REDACTED:email:u[0-9a-f]{11}\]"


def test_same_address_same_code_case_insensitive():
    assert email_code(KEY, "Bob@X.com") == email_code(KEY, "bob@x.com")
    assert re.fullmatch(r"u[0-9a-f]{11}", email_code(KEY, "bob@x.com"))


def test_different_addresses_different_codes():
    assert email_code(KEY, "bob@x.com") != email_code(KEY, "alice@x.com")


def test_different_keys_different_codes():
    assert email_code(b"one", "bob@x.com") != email_code(b"two", "bob@x.com")


def test_redact_without_key_keeps_the_plain_mask():
    assert redact("mail bob@x.com").text == "mail [REDACTED:email]"


def test_redact_with_key_adds_the_code():
    result = redact("mail bob@x.com", pseudonym_key=KEY)
    assert re.fullmatch(rf"mail {CODED_MASK}", result.text)
    assert "bob" not in result.text and "email" in result.kinds
    assert result.text == f"mail [REDACTED:email:{email_code(KEY, 'bob@x.com')}]"


def test_one_person_gets_one_code_everywhere_in_a_line():
    text = redact("a=Bob@x.com b=bob@x.com c=alice@x.com", pseudonym_key=KEY).text
    codes = re.findall(r"email:(u[0-9a-f]{11})", text)
    assert codes[0] == codes[1] != codes[2]


@pytest.mark.parametrize("text", [
    "login by bob@x.com failed", "password=hunter2 for bob@x.com", '{"user": "bob@x.com", "token": "abc123"}',
])
def test_redaction_is_idempotent_with_a_key(text):
    once = redact(text, pseudonym_key=KEY).text
    assert redact(once, pseudonym_key=KEY).text == once


def test_the_code_is_never_rewritten_by_the_card_rule():
    for n in range(300):
        text = redact(f"user{n}@example{n}.com", pseudonym_key=KEY).text
        assert re.fullmatch(CODED_MASK, text), text


def test_masks_with_a_code_survive_the_secret_scanner():
    text = "password=[REDACTED:email:u1a2b3c4d5e6] ok"
    assert redact(text, pseudonym_key=KEY).text == text


def settings(**values) -> Settings:
    return Settings(_env_file=None, database_url="postgresql://u@h/db", **values)


@pytest.mark.parametrize(("value", "expected"), [("k", b"k"), (None, None), ("  ", None), ("", None)])
def test_configured_pseudonym_key_reads_the_setting(monkeypatch, value, expected):
    monkeypatch.setattr(pseudonym, "get_settings", lambda: settings(pseudonym_key=value))
    assert configured_pseudonym_key() == expected
