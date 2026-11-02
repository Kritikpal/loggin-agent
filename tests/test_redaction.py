import json
import time

import pytest

from app.ingestion.redaction import _luhn_valid, redact
from tests.test_semantic_text import FIXTURES

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEF\n-----END RSA PRIVATE KEY-----"

# (log text, the secret inside it, the kind that must be reported)
SECRETS = [
    ("Authorization: Bearer abc123.def456", "abc123.def456", "auth"),
    ('authorization="Basic dXNlcjpwYXNz"', "dXNlcjpwYXNz", "auth"),
    ("Proxy-Authorization: Digest realm9876", "realm9876", "auth"),
    ("Cookie: session=abcdef123; theme=dark", "abcdef123", "cookie"),
    ("Set-Cookie: id=xyz789; HttpOnly", "xyz789", "cookie"),
    ("login failed password=hunter2 for admin", "hunter2", "secret"),
    ("db_password: hunter2", "hunter2", "secret"),
    ('{"password": "hunter2"}', "hunter2", "secret"),
    ("api_key='abc123xyz'", "abc123xyz", "secret"),
    ("X-Api-Key: abcdef123456", "abcdef123456", "secret"),
    ("client_secret=s3cr3tvalue&grant=code", "s3cr3tvalue", "secret"),
    ("access-key = AKIAXXXXXXXXXXXX1234", "AKIAXXXXXXXXXXXX1234", "secret"),
    (f"got {JWT} from client", JWT, "jwt"),
    ("key AKIAIOSFODNN7EXAMPLE leaked", "AKIAIOSFODNN7EXAMPLE", "aws_key"),
    ("using sk-abcdefghijklmnopqrstuvwxyz123456 now", "sk-abcdefghijklmnopqrstuvwxyz123456", "api_key"),
    ("using gsk_abcdefghijklmnopqrstuvwxyz123456 now", "gsk_abcdefghijklmnopqrstuvwxyz123456", "api_key"),
    (f"push with ghp_{'a1B2' * 9}", "ghp_" + "a1B2" * 9, "api_key"),
    (f"key follows\n{PEM}\nafter", "MIIEvQIBADANBgkqhkiG9w0BAQEF", "private_key"),
    ("connecting to jdbc:postgresql://admin:s3cretpw@db:5432/app", "s3cretpw", "credentials"),
    ("sent mail to alice.smith+logs@example.co.uk today", "alice.smith+logs@example.co.uk", "email"),
    ("card 4111 1111 1111 1111 declined", "4111 1111 1111 1111", "card"),
    ("card 4111-1111-1111-1111 declined", "4111-1111-1111-1111", "card"),
    ("card 4111111111111111 declined", "4111111111111111", "card"),
    ("amex 378282246310005 declined", "378282246310005", "card"),
]


@pytest.mark.parametrize("text, secret, kind", SECRETS)
def test_each_kind_of_secret_is_removed_and_reported(text, secret, kind):
    result = redact(text)

    assert secret not in result.text and kind in result.kinds and "[REDACTED:" in result.text


def test_surrounding_text_and_quotes_survive():
    assert redact("user=bob password=hunter2 ok").text == "user=bob password=[REDACTED:secret] ok"
    assert redact('"password": "hunter2"').text == '"password": "[REDACTED:secret]"'
    assert redact("Authorization: Bearer abc").text == "Authorization: [REDACTED:auth]"


def test_json_stays_valid_json():
    line = json.dumps({"msg": "hi", "password": "hunter2", "auth": {"token": "abc.def"}, "user": "u"})

    redacted = json.loads(redact(line).text)

    assert redacted["password"] == "[REDACTED:secret]" and redacted["auth"]["token"] == "[REDACTED:secret]"
    assert redacted["msg"] == "hi" and redacted["user"] == "u"


def test_several_secrets_on_one_line_are_all_removed():
    text = "password=aaa111 token=bbb222 mail me@x.com url http://u:ppp333@h/"

    result = redact(text)

    assert not any(s in result.text for s in ("aaa111", "bbb222", "me@x.com", "ppp333"))
    assert result.kinds == {"secret", "email", "credentials"}


@pytest.mark.parametrize("text", [
    "Fetching details for user ID: 4042", "tokenizer loaded", "a monkey with a keyboard", "order ORD-99128 placed",
    "Removed 0 expired sessions", "url=jdbc:h2:mem:testdb user=SA", "ratio 4111111111111112 not a card",
    "version 1.2.3 built 2026-10-29T08:15:23.412Z", "at com.acme.A.f(A.java:42)",
])
def test_ordinary_text_is_left_alone(text):
    assert redact(text).text == text


def test_timestamps_and_epochs_are_not_taken_for_card_numbers_even_when_the_checksum_passes():
    compact = next(f"20261029{n:06d}" for n in range(235959) if _luhn_valid(f"20261029{n:06d}"))
    epoch_ms = next(str(n) for n in range(1730000000000, 1730000001000) if _luhn_valid(str(n)))
    epoch_ns = next(str(n) for n in range(1730000000000000000, 1730000000000001000) if _luhn_valid(str(n)))

    for number in (compact, epoch_ms, epoch_ns):
        assert redact(f"at {number} done").text == f"at {number} done"


def test_redaction_never_returns_a_secret_in_the_kinds():
    assert all("hunter2" not in kind for kind in redact("password=hunter2").kinds)


@pytest.mark.parametrize("hostile", [
    "a=" * 500_000, "9" * 1_000_000, "9 " * 500_000, "token" * 200_000, "-----BEGIN PRIVATE KEY-----" * 40_000,
    "x" * 1_000_000, "x@" * 500_000, "://" * 300_000, "a:" * 500_000, "password=" * 100_000, "Cookie:" * 100_000,
    "eyJ" + "a" * 1_000_000, "http://" + "u" * 500_000 + ":",
], ids=lambda h: h[:12])
def test_hostile_input_is_processed_in_bounded_time(hostile):
    started = time.perf_counter()
    redact(hostile)

    assert time.perf_counter() - started < 3
