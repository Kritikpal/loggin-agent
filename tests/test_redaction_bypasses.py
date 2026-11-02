"""Regression tests for the security review of redact(): every case here once leaked a secret or broke JSON."""

import json
import re
import time

import pytest

from app.ingestion.redaction import redact
from app.ingestion.sanitize import sanitize
from tests.test_semantic_text import FIXTURES

CARD = "4111111111111111"


@pytest.mark.parametrize("text, secret", [
    ("password=hunter2[REDACTED:x]", "hunter2"),
    ("password=[REDACTED:foo]hunter2", "hunter2"),
    ('"password":"hunter2 [REDACTED:"', "hunter2"),
    ("token=[REDACTED:x]tail", "tail"),
])
def test_an_attacker_cannot_hide_a_secret_by_writing_the_mask_text_themselves(text, secret):
    assert secret not in redact(text).text


def test_a_real_mask_is_left_alone_and_nothing_else_is():
    assert redact("password=[REDACTED:secret] ok").text == "password=[REDACTED:secret] ok"


@pytest.mark.parametrize("text", [
    f"2026-10-29 {CARD}", f"ts 08:15:23 {CARD}", f"id=1234567 4111 1111 1111 1111", f"10 4111-1111-1111-1111",
    f"1 2 3 {CARD}", f"card_{CARD}", f"({CARD})", f"{CARD}.", "4111 1111 1111 1111 and 5500 0000 0000 0004",
])
def test_cards_are_found_wherever_they_sit_in_a_line(text):
    out = redact(text).text

    assert "card" in redact(text).kinds and not re.search(r"4111[ -]?1111[ -]?1111[ -]?1111|5500", out)


def test_dates_and_long_digit_runs_do_not_combine_into_cards():
    text = "2026-10-29 08:15:23 request 1730000000000 took 12345678901234567890123"

    assert redact(text).text == text


@pytest.mark.parametrize("document", [
    '{"password": ["hunter2","two"]}',
    '{"password": {"v":"hunter2"}}',
    '{"password":"a\\"hunter2"}',
    '{"msg":"{\\"password\\":\\"hunter2\\"}"}',
    '{"token": 123456, "other": 1}',
    '{"password": null, "other": 1}',
    '{"cookie": "a=1; hunter2", "user": "bob", "n": 1}',
    '{"Cookie":"sid=hunter2"}',
    '{"Authorization": "Bearer abc 123 hunter2", "user": "bob"}',
])
def test_json_stays_valid_and_leaks_nothing(document):
    redacted = redact(document).text

    json.loads(redacted)
    assert "hunter2" not in redacted and "123456" not in redacted


def test_neighbouring_json_fields_survive():
    parsed = json.loads(redact('{"cookie": "a=1", "user": "bob", "n": 1}').text)

    assert parsed["user"] == "bob" and parsed["n"] == 1


def test_a_closing_brace_after_an_unquoted_header_value_is_kept():
    assert redact("headers={Authorization=Bearer abc123}").text == "headers={Authorization=[REDACTED:auth]}"


@pytest.mark.parametrize("text, secret", [
    ("Authorization: Negotiate YIIabcdef", "YIIabcdef"),
    ('Authorization: Digest username="x", response="abcdef", nonce="n"', "abcdef"),
    ("Authorization: AWS4-HMAC-SHA256 Credential=AKIA/2020/x, Signature=deadbeef", "deadbeef"),
    ('Authorization: "Bearer abc 123"', "123"),
    ("Authorization: Bearer abc.def ghi", "ghi"),
])
def test_authorization_values_are_removed_whole(text, secret):
    assert secret not in redact(text).text


@pytest.mark.parametrize("text, secret", [
    ("Authorization : Bearer abc123", "abc123"),
    ("Authorization: Bearer abc123", "abc123"),
    ("password = hunter2", "hunter2"),
    ("pass​word=hunter2", "hunter2"),
    ("p­assword=hunter2", "hunter2"),
    ("ｐassword=hunter2", "hunter2"),
])
def test_unusual_spacing_and_invisible_characters_do_not_hide_a_key(text, secret):
    assert secret not in redact(text).text


@pytest.mark.parametrize("text, secret", [
    ("passphrase=correcthorse", "correcthorse"),
    ("private_key=abcdef123456", "abcdef123456"),
    ("SK-abcdefghijklmnopqrstuvwxyz123456", "abcdefghijklmnopqrstuvwxyz123456"),
    ("GSK_abcdefghijklmnopqrstuvwxyz123456", "abcdefghijklmnopqrstuvwxyz123456"),
    ("github_pat_11ABCDEFG0abcdefghijklmnop", "11ABCDEFG0abcdefghijklmnop"),
    ("glpat-abcdefghijklmnopqrst", "abcdefghijklmnopqrst"),
    ("AIDAIOSFODNN7EXAMPLE", "AIDAIOSFODNN7EXAMPLE"),
    ("http://user:p@ss:w0rd@host/path", "w0rd"),
])
def test_more_key_names_vendor_prefixes_and_awkward_url_passwords(text, secret):
    assert secret not in redact(text).text


PEMS = {
    "encrypted": "-----BEGIN ENCRYPTED PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,ABCDEF\n\n"
                 "MIIabcdefghijklmnopqrstuvwxyz\n-----END ENCRYPTED PRIVATE KEY-----",
    "pgp": "-----BEGIN PGP PRIVATE KEY BLOCK-----\nVersion: GnuPG v2\n\nlQOYBFabcdefghijklmnopqrstuv\n=ab12\n"
           "-----END PGP PRIVATE KEY BLOCK-----",
    "crlf": "-----BEGIN PRIVATE KEY-----\r\nMIIabcdefghijklmnopqrstuvwxyz\r\n-----END PRIVATE KEY-----",
    "no end": "-----BEGIN PRIVATE KEY-----\nMIIabcdefghijklmnopqrstuvwxyz",
}


@pytest.mark.parametrize("name", PEMS)
def test_private_key_blocks_are_removed_whole(name):
    out = redact(f"before\n{PEMS[name]}\nafter").text

    assert "abcdefghijklmnop" not in out and "END" not in out and out.startswith("before") and out.endswith("after")


def test_a_key_without_an_end_line_does_not_eat_the_next_log_line():
    out = redact("-----BEGIN PRIVATE KEY-----\n2020-01-01 INFO MIIabc").text

    assert out.endswith("2020-01-01 INFO MIIabc") and "[REDACTED:private_key]" in out


@pytest.mark.parametrize("hostile", [
    "password=[" * 100_000, 'password="' * 100_000, "{\"password\": " * 80_000, "444 " * 250_000, "4111-" * 200_000,
    "4 " * 500_000, "-----BEGIN PRIVATE KEY----- " * 30_000, "Authorization:" * 100_000, "token=\\\"" * 100_000,
    "-----BEGIN PRIVATE KEY-----\n" + "A" * 1_000_000, "\n".join(["4" * 20] * 45_000), "://a:" * 200_000,
], ids=lambda h: h[:14].replace("\n", " "))
def test_more_hostile_input_is_still_processed_in_bounded_time(hostile):
    started = time.perf_counter()
    redact(hostile)

    assert time.perf_counter() - started < 3


@pytest.mark.parametrize(
    "hostile",
    ["\x1b]" * 300_000, "\x1b]0;" + "a" * 1_000_000, "\x1bP" * 300_000, "\x1b[" * 300_000],
    ids=["many-osc", "long-osc", "many-dcs", "many-csi"],
)
def test_sanitize_is_bounded_on_hostile_escape_sequences(hostile):
    started = time.perf_counter()
    sanitize(hostile)

    assert time.perf_counter() - started < 3


@pytest.mark.parametrize("text, expected", [
    ("a\x1b]0;title\nSECRET\x07", "a\nSECRET"),  # an unterminated OSC ends with its line instead of eating what follows
    ("x\x1bP1;1|abc\x1b\\y", "xy"),
    ("a\ud800b", "ab"),
    ("a b c", "abc"),
    ("a‮b﻿c​d", "abcd"),
    ("a\x00b", "ab"),
])
def test_sanitize_removes_what_breaks_postgres_or_hides_text(text, expected):
    assert sanitize(text) == expected
