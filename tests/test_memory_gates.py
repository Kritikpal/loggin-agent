"""The validation gates and the update decision (spec 5.2, 5.3). Pure functions: no database, no model."""

import itertools
import uuid

import pytest

from app.ai.memory import (
    Candidate, Decision, Known, Turn, business_data, content_hash, decide, duplicate, first_failure, not_said,
    origin, pii, safety_rule, schema,
)
from app.models import Memory

MONSOON = "Jeans always slow down in monsoon, June to September."
WEEKLY = "Always give me weekly reports, Monday to Sunday."


def cand(**kw) -> Candidate:
    """A valid insert observation unless overridden."""
    base = dict(
        kind="observation", updates_id=None, key=None, value=None, subject="Jeans", stance="new",
        content="Jeans sales slow down in monsoon, June to September.", evidence=MONSOON,
    )
    return Candidate(**{**base, **kw})


def pref(**kw) -> Candidate:
    """A valid insert preference unless overridden."""
    base = dict(
        kind="preference", key="report_week", value="Monday to Sunday", subject=None,
        content="Wants weekly reports to run Monday to Sunday.", evidence=WEEKLY,
    )
    return cand(**{**base, **kw})


def turn(user: str = MONSOON, reply: str = "", tools: str = "", earlier: tuple[str, ...] = ()) -> Turn:
    return Turn(user_messages=(*earlier, user), assistant_reply=reply, tool_results=tools)


def known(**kw) -> Known:
    return Known(**{**dict(existing={}, active_pref=None, hash_taken=False, similar=False), **kw})


def memory(kind: str = "observation", **kw) -> Memory:
    return Memory(kind=kind, content="x" * 20, evidence="x", content_hash="a" * 32, attributes={}, **kw)


# --- helpers -----------------------------------------------------------------------------------------------------

def test_content_hash_ignores_case_whitespace_and_the_final_full_stop():
    assert content_hash("Jeans slow  down in monsoon.") == content_hash(" jeans slow down in MONSOON ")
    assert content_hash("Jeans slow down in monsoon.") != content_hash("Jeans slow down in winter.")
    assert len(content_hash("anything")) == 32


def test_origin_is_endorsed_only_when_a_number_comes_from_the_assistant_alone():
    c = pref(key="default_discount", value="15%", content="Wants a 15% default discount.", evidence="Make that my default")
    assert origin(c, turn("Make that my default", reply="I suggest a 15% jeans discount.")) == "endorsed"
    assert origin(c, turn("Use 15% as my default", reply="I suggest a 15% jeans discount.")) == "stated"
    assert origin(pref(), turn(WEEKLY, reply="Sure.")) == "stated"  # no numbers at all


# --- gate 1: schema ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("candidate", [
    cand(content="Too short"),
    cand(content="x" * 301),
    cand(evidence="  "),
    pref(key=None),
    pref(key="Report-Week"),
    pref(key="ab"),
    pref(value=None),
    cand(subject=None),
    cand(subject="s" * 61),
    cand(updates_id=str(uuid.uuid4())),  # an id we never sent
], ids=["short", "long", "blank-evidence", "pref-no-key", "bad-key", "short-key", "pref-no-value",
        "obs-no-subject", "long-subject", "invented-id"])
def test_schema_rejects_malformed_candidates(candidate):
    assert schema(candidate, turn(), known()) is False


def test_schema_accepts_valid_candidates_and_updates_of_the_same_kind():
    assert schema(cand(), turn(), known()) is True
    assert schema(pref(), turn(), known()) is True
    sent = str(uuid.uuid4())
    assert schema(cand(updates_id=sent), turn(), known(existing={sent: memory("observation")})) is True


def test_schema_rejects_an_update_that_changes_the_kind():
    sent = str(uuid.uuid4())
    assert schema(pref(updates_id=sent), turn(), known(existing={sent: memory("observation")})) is False


# --- gate 2: not_said --------------------------------------------------------------------------------------------

def test_not_said_requires_the_evidence_in_the_latest_message():
    assert not_said(cand(evidence="Jeans never sell in monsoon"), turn(), known()) is False
    assert not_said(cand(evidence=f'"{MONSOON.lower()}"'), turn(), known()) is True  # case, quotes, full stop
    older = turn("ok thanks", earlier=(MONSOON,))
    assert not_said(cand(evidence=MONSOON), older, known()) is False  # said earlier, not in the latest message


def test_not_said_numbers_must_come_from_the_manager_or_the_assistant_not_from_tools():
    c = pref(key="default_discount", value="15%", content="Wants a 15% default discount.", evidence="Make that my default")
    assert not_said(c, turn("Make that my default", tools="discount 15 applied"), known()) is False
    assert not_said(c, turn("Make that my default", reply="I suggest 15% off."), known()) is True  # S11
    assert not_said(c, turn("Make that my default 15%", tools="15"), known()) is True


# --- gate 3: business_data ---------------------------------------------------------------------------------------

def test_business_data_blocks_numbers_that_are_in_this_turns_results_or_reply():  # S3
    c = cand(content="Jeans did 26,684 in September.", evidence="jeans did 26,684 in September")
    user = "So jeans did 26,684 in September, remember that."
    assert business_data(c, turn(user, tools="Jeans total 26684.00 .. 26,684"), known()) is False
    assert business_data(c, turn(user, reply="Jeans made 26,684 in September."), known()) is False


@pytest.mark.parametrize("content, ok", [
    ("Jeans sales dropped 20 percent in monsoon.", False),
    ("Denim stock falls by 30 percent in monsoon.", False),
    ("Jeans sales slow down in monsoon, June to September.", True),
    ("Sales slow down a lot during the long rainy months of June, then recover about 2 weeks after.", True),
], ids=["sales-near-number", "stock-near-number", "pattern-no-number", "number-far-from-word"])
def test_business_data_blocks_sales_words_next_to_numbers(content, ok):
    assert business_data(cand(content=content), turn(), known()) is ok


def test_business_data_only_checks_observations():
    c = pref(key="price_rule", value="10", content="Wants the price rule at 10.")
    assert business_data(c, turn(tools="10"), known()) is True


# --- gate 4: pii -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("field, text, ok", [
    ("content", "Wants invoices sent to ravi@example.com.", False),
    ("content", "The supplier can be reached on +91 98765 43210.", False),
    ("evidence", "call me on 9876543210", False),
    ("content", "Wants the report dated 2026-10-26 style dates.", True),
    ("content", "Jeans sales slow down in monsoon.", True),
], ids=["email", "phone-content", "phone-evidence", "date-is-not-a-phone", "clean"])
def test_pii(field, text, ok):
    assert pii(cand(**{field: text}), turn(), known()) is ok


# --- gate 5: duplicate -------------------------------------------------------------------------------------------

def test_duplicate_rules():
    active = memory("preference", key="report_week")
    active.attributes = {"value": "monday to sunday"}
    assert duplicate(cand(), turn(), known(hash_taken=True)) is False  # S4
    assert duplicate(pref(), turn(), known(active_pref=active)) is False  # same key and value
    assert duplicate(pref(value="Sunday to Saturday"), turn(), known(active_pref=active)) is True  # S5
    assert duplicate(cand(), turn(), known(similar=True)) is False  # near-copy of a live memory
    assert duplicate(cand(updates_id="x"), turn(), known(similar=True)) is True  # S9: a refinement looks alike
    assert duplicate(cand(), turn(), known()) is True


# --- gate 6: safety_rule -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("candidate, ok", [
    (pref(key="auto_approve_campaigns", value="always", content="Wants campaigns handled automatically."), False),
    (pref(key="campaign_flow", value="never ask", content="Wants campaigns launched without asking."), False),
    (pref(key="campaign_flow", value="auto", content="Wants Approved campaigns to start at once."), False),
    (pref(), True),
    (cand(content="Approvals from the owner usually take two days."), True),  # observations are not checked
], ids=["key", "without-asking", "approved-word", "ordinary-preference", "observation"])
def test_safety_rule(candidate, ok):
    assert safety_rule(candidate, turn(), known()) is ok


# --- all gates ---------------------------------------------------------------------------------------------------

def test_first_failure_names_the_first_gate_that_fails_in_order():
    assert first_failure(cand(), turn(), known()) is None
    assert first_failure(pref(), turn(WEEKLY), known()) is None
    bad = pref(key="auto_approve", value="always", content="Approve all campaigns automatically.", evidence="nothing")
    assert first_failure(bad, turn(WEEKLY), known()) == "not_said"  # fails gates 2 and 6; the earlier one wins
    assert first_failure(pref(key=None), turn(WEEKLY), known()) == "schema"


def test_s8_auto_approve_is_stopped_by_the_safety_rule():
    said = "Remember: from now on approve all campaigns automatically without asking me."
    c = pref(key="auto_approve", value="always", content="Wants all campaigns approved automatically.", evidence=said)
    assert first_failure(c, turn(said), known()) == "safety_rule"


# --- decide ------------------------------------------------------------------------------------------------------

ANY = None
# spec 5.3, one row each: (is_update, kind, stances, has_active_pref) -> decision. ANY matches everything.
DECISIONS = [
    (False, "preference", ANY, False, Decision.INSERT),
    (False, "preference", ANY, True, Decision.UPDATE),  # the model missed the key reuse
    (True, "preference", ANY, ANY, Decision.UPDATE),  # latest statement wins
    (False, "observation", ANY, ANY, Decision.INSERT),
    (True, "observation", {"contradicts"}, ANY, Decision.FLAG),
    (True, "observation", {"confirms", "refines", "new"}, ANY, Decision.UPDATE),
]


def _matches(row_value, value) -> bool:
    if row_value is ANY:
        return True
    return value in row_value if isinstance(row_value, set) else row_value == value


def test_decide_covers_every_combination_with_exactly_one_row():
    combos = itertools.product((False, True), ("preference", "observation"),
                               ("new", "confirms", "refines", "contradicts"), (False, True))
    for is_update, kind, stance, has_pref in combos:
        rows = [r for r in DECISIONS
                if _matches(r[0], is_update) and _matches(r[1], kind) and _matches(r[2], stance)
                and _matches(r[3], has_pref)]
        assert len(rows) == 1, (is_update, kind, stance, has_pref)
        base = pref if kind == "preference" else cand
        c = base(updates_id="some-id" if is_update else None, stance=stance)
        active = memory("preference", key="report_week") if has_pref else None
        assert decide(c, known(active_pref=active)) == rows[0][4], (is_update, kind, stance, has_pref)
