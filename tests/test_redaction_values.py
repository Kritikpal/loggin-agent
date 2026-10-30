"""How far a secret's value extends: strings with escapes, nested brackets, unterminated and unbalanced values."""

import json

import pytest

from app.ingestion.redaction import redact


@pytest.mark.parametrize("text, expected", [
    # a bracketed value containing strings with escaped quotes and brackets is masked whole
    ('{"token": ["a\\"]", {"k": "}"}], "next": 1}', '{"token": "[REDACTED:secret]", "next": 1}'),
    # an unterminated quote ends with its line, so the following lines are untouched
    ("password='abc\nnext line", "password='[REDACTED:secret]'\nnext line"),
    ('password="abc', 'password="[REDACTED:secret]"'),
    # an unbalanced bracket ends with its line too
    ("password=[abc\nnext line", "password=[REDACTED:secret]\nnext line"),
    ("token={abc, more\nnext", "token=[REDACTED:secret]\nnext"),
    # a value glued to a closing bracket is part of it
    ("password=[a]b tail", "password=[REDACTED:secret] tail"),
    # an empty value masks nothing
    ("password=\nnext", "password=\nnext"),
    ("password=", "password="),
    # an escaped-quote string with no closing escaped quote ends with its line
    ('{\\"password\\":\\"abc\nnext', '{\\"password\\":\\"[REDACTED:secret]\\"\nnext'),
    # a single-quoted value with an escaped quote
    ("password='it\\'s secret' ok", "password='[REDACTED:secret]' ok"),
])
def test_the_extent_of_a_secret_value(text, expected):
    assert redact(text).text == expected


def test_redacted_json_with_nested_secrets_still_parses():
    document = json.dumps({"a": {"password": ["x", {"y": "z"}], "token": 5, "ok": True}, "b": [1, 2]})

    parsed = json.loads(redact(document).text)

    assert parsed["a"]["password"] == "[REDACTED:secret]" and parsed["a"]["token"] == "[REDACTED:secret]"
    assert parsed["a"]["ok"] is True and parsed["b"] == [1, 2]
