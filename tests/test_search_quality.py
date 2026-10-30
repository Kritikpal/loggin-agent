"""Acceptance measurement AC9: retrieval quality of search_logs on the real log with the real embedding model.
Slow (loads bge-small); run with  .venv/Scripts/python -m pytest -m slow -s tests/test_search_quality.py"""

from collections import defaultdict
from dataclasses import dataclass

import pytest

from tests.search_helpers import (
    DEV_AUTH_LOG, KINDS, GoldQuery, copy_fixture, dev_auth_source, hit_lines, index_file, load_gold_set, search,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

TOP = 5
RECALL_TARGET = 0.85  # AC9
IDENTIFIER_MAX_RANK = 3


@dataclass(frozen=True)
class Outcome:
    query: GoldQuery
    lines: tuple[int, ...]  # hit line_starts in rank order

    @property
    def first_rank(self) -> int | None:
        return next((i for i, n in enumerate(self.lines, 1) if n in self.query.expect_lines), None)

    @property
    def answered(self) -> bool:
        return self.first_rank is not None and self.first_rank <= TOP


def report(outcomes: list[Outcome]) -> dict[str, float]:
    by_kind: dict[str, list[Outcome]] = defaultdict(list)
    for outcome in outcomes:
        by_kind[outcome.query.kind].append(outcome)
    recall = lambda group: sum(o.answered for o in group) / len(group)  # noqa: E731
    mrr = sum(1 / o.first_rank for o in outcomes if o.first_rank) / len(outcomes)

    print(f"\n{'id':32} {'kind':10} {'ok':3} {'rank':>4}  top-3 lines   expected")
    for o in outcomes:
        print(f"{o.query.id:32} {o.query.kind:10} {'Y' if o.answered else '-':3} {o.first_rank or '-':>4}  "
              f"{str(list(o.lines[:3])):13} {list(o.query.expect_lines)}")
    stats = {"overall": recall(outcomes), **{k: recall(by_kind[k]) for k in KINDS}, "mrr": mrr}
    print("  ".join(f"Recall@{TOP} {k}={v:.2f}" if k != "mrr" else f"MRR={v:.3f}" for k, v in stats.items()))
    return stats


async def test_recall_at_5_on_the_gold_set_meets_the_acceptance_target(tmp_path, session_factory, capsys):
    from app.core.embeddings import embed_documents, embed_query

    source = dev_auth_source(copy_fixture(DEV_AUTH_LOG, tmp_path))
    indexed = await index_file(source, session_factory, embed=embed_documents)
    assert indexed.embedded == indexed.events_stored == 30

    outcomes = []
    for query in load_gold_set():
        response = await search(session_factory, [source], query.query, embed=embed_query, top_k=10)
        outcomes.append(Outcome(query, tuple(hit_lines(response))))
    with capsys.disabled():  # the table is the point of this test: show it even without -s
        stats = report(outcomes)

    slow_identifiers = [o.query.id for o in outcomes
                        if o.query.kind == "identifier" and (o.first_rank or 99) > IDENTIFIER_MAX_RANK]
    assert stats["overall"] >= RECALL_TARGET, f"Recall@{TOP} {stats['overall']:.2f} < {RECALL_TARGET}"
    assert slow_identifiers == [], "identifier queries must rank an expected event in the top 3 (keyword leg)"
