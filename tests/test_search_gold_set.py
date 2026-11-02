"""The labelled questions for search_logs and the ground truth they rest on. Nothing here needs the tool, so these
tests pass now: they prove the gold set and the shared helpers are right before the tool exists."""

import hashlib
from collections import Counter

import pytest
from sqlalchemy import select

from app.models import LogEvent
from tests.search_helpers import (
    DEV_AUTH_EVENTS, DEV_AUTH_ID, DEV_AUTH_LOG, ERROR_LINES, KINDS, WARN_LINES, copy_fixture, dev_auth_source,
    event_start_lines, fake_embed_documents, fake_vector, far, index_file, load_gold_set, near,
)


def test_the_fake_embedder_gives_deterministic_unit_vectors():
    a, b = fake_vector("why did startup fail"), fake_vector("why did startup fail")
    dot = lambda x, y: sum(p * q for p, q in zip(x, y))  # noqa: E731

    assert a == b and len(a) == 384 and dot(a, a) == pytest.approx(1.0)
    assert abs(dot(a, fake_vector("something else"))) < 0.3
    assert dot(a, near(a, 1)) > 0.95 and dot(a, far(a, 1)) < -0.95
    assert fake_embed_documents(["x", "y"]) == [fake_vector("x"), fake_vector("y")]
