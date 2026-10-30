import json
import random
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from app.ingestion.fields import ResolvedEvent, resolve
from app.ingestion.sanitize import sanitize
from app.ingestion.semantic_text import ParsedEvent, build_semantic_text
from app.ingestion.sources import FileSource
from tests.test_field_resolver import assert_provenance_complete

TEXT = FileSource(id="text", path=Path("app.log"))
JSON = FileSource(id="json", path=Path("app.log"), format="json")
SOURCES = {"text": TEXT, "json": JSON}
MAX_SECONDS = 2.0


def strings_in(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in strings_in(k) + strings_in(v)]
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in strings_in(item)]
    return []


def everything_but_raw(result: ResolvedEvent) -> list[str]:
    parsed = {k: v for k, v in asdict(result.parsed).items() if k != "raw_text"}
    provenance = {name: asdict(p) for name, p in result.provenance.items()}
    return strings_in([parsed, provenance, result.fields])


def assert_storable(result: ResolvedEvent) -> None:
    """PostgreSQL text/jsonb reject NUL and lone surrogates; UTF-8 encoding fails on the latter."""
    for text in everything_but_raw(result):
        assert "\x00" not in text and not any("\ud800" <= ch <= "\udfff" for ch in text)
    json.dumps([asdict(result.parsed)["extra"], result.fields], ensure_ascii=False, allow_nan=False).encode("utf-8")


def assert_fallback(result: ResolvedEvent, text: str) -> None:
    assert result.parsed == ParsedEvent(raw_text=text)
    assert result.occurred_at is None and result.fields == {}
    assert list(result.provenance) == ["_resolver"] and result.provenance["_resolver"].method == "fallback"


@pytest.mark.parametrize("text", [
    "", "   ", "\n", "{", '{"a": 1', "not json", '{"a": 1} trailing', "[1, 2]", "42", '"text"', "null", "true",
    "[" * 10_000, '{"a":' * 10_000, "{" * 10_000,
], ids=["empty", "spaces", "newline", "brace", "unclosed", "prose", "trailing", "array", "number", "string", "null",
        "true", "deep-list", "deep-object", "deep-brace"])
def test_unusable_json_falls_back_to_the_raw_text_without_raising(text):
    result = resolve(text, JSON)

    assert_fallback(result, text)
    assert result.provenance["_resolver"].note in {"JSONDecodeError", "RecursionError", "not_a_json_object"}
    assert build_semantic_text(result.parsed).text == text.strip()[:1500]


def test_the_fallback_note_names_the_failure_class_not_the_content():
    assert resolve("{secret-ish content", JSON).provenance["_resolver"].note == "JSONDecodeError"
    assert resolve("[1]", JSON).provenance["_resolver"].note == "not_a_json_object"


def test_an_unexpected_error_still_gives_the_fallback():
    broken = FileSource(id="json", path=Path("x"), format="json", timezone="Not/AZone")

    result = resolve('{"time": "2026-10-29T08:15:23"}', broken)

    assert_fallback(result, '{"time": "2026-10-29T08:15:23"}')


def test_valid_but_deep_nesting_is_cut_at_the_depth_limit():
    text = '{"a":' * 50 + '"bottom"' + "}" * 50

    result = resolve(text, JSON)

    depth, node = 0, result.fields
    while isinstance(node, dict):
        depth, node = depth + 1, node["a"]
    assert depth <= 9 and isinstance(node, str) and node != "bottom"


@pytest.mark.parametrize("make", [
    lambda: json.dumps({"message": "x" * 1_000_000, "level": "info"}),
    lambda: json.dumps({f"k{i:05d}": i for i in range(20_000)} | {"msg": "m"}),
    lambda: json.dumps({f"k{i:04d}": "v" * 3000 for i in range(300)}),
    lambda: json.dumps({"list": list(range(200_000))}),
    lambda: json.dumps({"n": int("9" * 4000), "f": 1e308, "huge": "1" * 5000}),
    lambda: '{"n": 1e999, "m": NaN, "i": -Infinity}',
], ids=["huge-string", "many-keys", "many-long-values", "long-list", "huge-numbers", "non-finite"])
def test_huge_json_is_bounded_and_fast(make):
    text = make()
    started = time.perf_counter()

    result = resolve(text, JSON)

    assert time.perf_counter() - started < MAX_SECONDS
    assert result.parsed.raw_text is text and len(result.parsed.extra) <= 50
    assert len(json.dumps(result.fields, allow_nan=False)) < 40_000
    assert all(len(s) <= 8192 for s in everything_but_raw(result))
    assert_storable(result)


@pytest.mark.parametrize("text", [
    "\n" * 1_000_000, " " * 1_000_000 + "Exception", "a" * 1_000_000, "X: " + " " * 1_000_000,
    "trace_id=" * 100_000, ("2026-10-29T08:15:23Z ERROR " + "Foo" * 30 + "Exception\n") * 20_000,
    "\tat a.B(B.java:1)\n" * 60_000, "[" * 1_000_000, "a" * 500_000 + "Exception: x",
], ids=["newlines", "spaces", "letters", "colon-spaces", "trace-keys", "repeated-events", "frames", "brackets",
        "late-exception"])
def test_a_hostile_megabyte_of_text_is_fast(text):
    for source in (TEXT, JSON):
        started = time.perf_counter()

        result = resolve(text, source)

        assert time.perf_counter() - started < MAX_SECONDS
        assert result.parsed.raw_text is text
        trace = result.parsed.stack_trace or ""  # a text trace keeps up to 64 KB so its last "Caused by" survives
        assert len(trace) <= 64 * 1024
        assert all(len(s) <= 8192 for s in everything_but_raw(result) if s != trace)


@pytest.mark.parametrize("obj", [
    {f"k{i:03d}": {"a": [1, 2]} for i in range(300)},
    {"list": ["v" * 2000] * 50},
], ids=["many-objects", "long-list-items"])
def test_the_stored_fields_stop_cleanly_at_the_budget(obj):
    result = resolve(json.dumps(obj), JSON)

    assert 0 < len(json.dumps(result.fields)) < 40_000


def test_a_one_line_exception_string_and_an_over_long_timestamp():
    result = resolve(json.dumps({"error": "java.lang.OutOfMemoryError", "time": "2026-10-29T08:15:23Z" + "0" * 60}), JSON)

    assert result.parsed.exception_type == "java.lang.OutOfMemoryError"
    assert (result.parsed.exception_message, result.parsed.stack_trace, result.occurred_at) == (None, None, None)


def test_json_escapes_for_nul_surrogates_and_ansi_are_sanitised_everywhere():
    text = (
        '{"message": "a\\u0000b\\ud800c", "k\\u0000ey\\udfff": "\\u001b[31mred\\u001b[0m", "level": "info\\u0000",'
        ' "nested": {"x": ["\\ud800", "\\u0000"], "\\u202eevil": "\\u200bz"}, "logger": "\\udc00"}'
    )

    result = resolve(text, JSON)

    assert_storable(result)
    assert (result.parsed.message, result.parsed.level, result.parsed.logger) == ("abc", "INFO", None)
    assert result.fields == {"key": "red", "logger": "", "nested": {"evil": "z", "x": ["", ""]}}


def random_text(rng: random.Random) -> str:
    pieces = [
        "{", "}", "[", "]", '"', "\\", ":", ",", "\n", "\t", " ", "\x00", "\x1b[31m", "\ud800", "\udfff", "‮",
        "ERROR", "info", "level", "2026-10-29T08:15:23Z", "Exception", "java.lang.X", "trace_id=", "1e999", "é", "😀",
        '"level": 50', '"time": 1793261723000', "NaN", "null",
    ]
    size = rng.choice([0, 1, 5, 30, 200, 3000])
    return "".join(rng.choice(pieces) if rng.random() < 0.5 else chr(rng.randrange(0x110000)) for _ in range(size))


def random_json(rng: random.Random, depth: int = 0) -> object:
    kind = rng.randrange(6 if depth < 4 else 4)
    if kind == 0:
        return random_text(rng)[:50]
    if kind == 1:
        return rng.choice([0, 30, 50, -1, 10**15, 1793261723, 1.5, True, None])
    if kind == 2:
        return rng.choice(["2026-10-29 08:15:23", "WARNING", "java.io.IOException: x\n\tat a", ""])
    if kind == 3:
        return None
    if kind == 4:
        return [random_json(rng, depth + 1) for _ in range(rng.randrange(4))]
    keys = ["level", "msg", "time", "error", "mdc", "trace.id", "Level", random_text(rng)[:10]]
    return {rng.choice(keys): random_json(rng, depth + 1) for _ in range(rng.randrange(6))}


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_fuzzed_input_never_raises_and_always_keeps_the_raw_text(fmt):
    rng = random.Random(f"resolver-{fmt}")
    started = time.perf_counter()
    for i in range(2000):
        text = random_text(rng) if i % 2 else json.dumps(random_json(rng))
        for candidate in (text, sanitize(text)):
            result = resolve(candidate, SOURCES[fmt])
            assert isinstance(result, ResolvedEvent) and result.parsed.raw_text is candidate
            assert_provenance_complete(result)
        if fmt == "json":
            assert_storable(result)
        build_semantic_text(result.parsed)
    assert time.perf_counter() - started < 30
