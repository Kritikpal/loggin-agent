"""Turns one JSON log line into a bounded, sanitised tree and gives lookups into it.

Log content is untrusted: depth, node count, string length and total characters are capped, and every key and string
goes through sanitize() because JSON escapes (\\u0000, lone surrogates, ANSI) survive the earlier text-level pass and
would break PostgreSQL text/jsonb. Non-finite and oversized numbers are kept as strings, since jsonb rejects NaN.
"""

import json
import math
from collections import deque
from dataclasses import dataclass

from app.ingestion.sanitize import sanitize

MAX_DEPTH = 8
MAX_LIST_ITEMS = 50
MAX_NESTED_KEYS = 200  # per nested object; the top-level object is limited only by the node budget
MAX_KEY_CHARS = 256
MAX_INT_DIGITS = 30
MAX_EXTRA_KEYS = 50
MAX_EXTRA_VALUE_CHARS = 200
TOO_DEEP = "[nested too deep]"
REDACTED_MARK = "[REDACTED:"

Path = tuple[str, ...]


@dataclass(frozen=True)
class Limits:
    nodes: int  # keys, values and containers together
    chars: int  # of keys and strings together
    string_chars: int  # per string


# Parsing bounds the work; the fields that are stored are bounded again, more tightly, after extraction.
PARSE_LIMITS = Limits(nodes=2000, chars=512 * 1024, string_chars=8192)
FIELD_LIMITS = Limits(nodes=200, chars=32 * 1024, string_chars=2048)

_CUT = object()  # out of budget: stop adding to the enclosing container


class _Budget:
    """What one cleaning pass may still produce. Mutable on purpose and private to this module."""

    def __init__(self, limits: Limits) -> None:
        self.nodes, self.chars, self.string_chars = limits.nodes, limits.chars, limits.string_chars

    def node(self) -> bool:
        self.nodes -= 1
        return self.nodes >= 0

    def text(self, value: str, cap: int) -> object:
        if not self.node() or self.chars <= 0:
            return _CUT
        clipped = sanitize(value[:min(cap, self.chars)])
        self.chars -= len(clipped)
        return clipped


def parse_object(text: str) -> dict | None:
    """The event as a JSON object, or None when it is valid JSON of another type. Raises ValueError or
    RecursionError on malformed or absurdly nested input."""
    tree = json.loads(text, parse_int=_parse_int, parse_float=_parse_float, parse_constant=str)
    return tree if isinstance(tree, dict) else None


def _parse_int(digits: str) -> int | str:
    return int(digits) if len(digits) <= MAX_INT_DIGITS else digits


def _parse_float(digits: str) -> float | str:
    number = float(digits)
    return number if math.isfinite(number) else digits


def clean(tree: dict, limits: Limits) -> dict:
    """A bounded, sanitised copy. Keys are visited in sorted order, so the result does not depend on key order, and
    level by level (breadth first), so when an event is too big it is the deep, bulky parts that are cut: a huge `mdc`
    object can never use up the budget before the top-level `message` or `trace_id` is reached."""
    budget, root = _Budget(limits), {}
    queue: deque[tuple[object, dict | list, int]] = deque([(tree, root, 0)])  # (source, its clean copy, depth)
    while queue:
        source, target, depth = queue.popleft()
        if isinstance(source, dict):  # a nested object keeps its first keys only, so it cannot starve its siblings
            members = sorted(source.items())[:None if depth == 0 else MAX_NESTED_KEYS]
        else:
            members = enumerate(source[:MAX_LIST_ITEMS])
        for key, item in members:
            if not _add(target, key, item, budget, depth, queue):
                break
    return root


def _add(target: dict | list, key: object, item: object, budget: _Budget, depth: int, queue: deque) -> bool:
    """Copy one member into `target`; False when the budget ran out (the rest of this container is dropped)."""
    name = budget.text(key, MAX_KEY_CHARS) if isinstance(target, dict) else None
    if name is _CUT:
        return False
    if isinstance(item, str):
        value = budget.text(item, budget.string_chars)
    elif not isinstance(item, (dict, list)):
        value = item if budget.node() else _CUT
    elif depth + 1 >= MAX_DEPTH:
        value = budget.text(TOO_DEEP, len(TOO_DEEP))
    elif budget.node():
        value = {} if isinstance(item, dict) else []
        queue.append((item, value, depth + 1))
    else:
        value = _CUT
    if value is _CUT:
        return False
    if isinstance(target, dict):
        target[name] = value  # two keys equal after sanitising: the later one in sorted order wins
    else:
        target.append(value)
    return True


def index_paths(tree: dict) -> dict[str, tuple[Path, object]]:
    """Every object member by its lower-cased dotted path. A flat "log.level" key and a nested log -> level both
    answer "log.level"; the first in sorted order wins, which keeps the choice deterministic."""
    found: dict[str, tuple[Path, object]] = {}

    def walk(node: dict, prefix: Path) -> None:
        for key, value in node.items():
            path = prefix + (key,)
            found.setdefault(".".join(path).lower(), (path, value))
            if isinstance(value, dict):
                walk(value, path)

    walk(tree, ())
    return found


def without(tree: dict, paths: frozenset[Path], prefix: Path = ()) -> dict:
    """A copy without the given members; objects emptied by the removal disappear too."""
    out: dict[str, object] = {}
    for key, value in tree.items():
        path = prefix + (key,)
        if path in paths:
            continue
        if isinstance(value, dict) and any(p[:len(path)] == path for p in paths):
            value = without(value, paths, path)
            if not value:
                continue
        out[key] = value
    return out


def scalar_view(tree: dict) -> dict[str, object]:
    """Dotted keys to short scalar values, for ParsedEvent.extra. Masked secrets and lists are left out."""
    flat: dict[str, object] = {}

    def walk(node: dict, prefix: str) -> None:
        for key, value in node.items():
            name = prefix + key
            if isinstance(value, dict):
                walk(value, name + ".")
            elif isinstance(value, str) and value.strip() and REDACTED_MARK not in value:
                flat[name] = value[:MAX_EXTRA_VALUE_CHARS]
            elif isinstance(value, (bool, int, float)):
                flat[name] = value

    walk(tree, "")
    return dict(sorted(flat.items())[:MAX_EXTRA_KEYS])
