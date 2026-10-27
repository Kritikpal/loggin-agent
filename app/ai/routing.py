"""Keyword pre-check that lets obvious questions skip the router's model call.

It can only say "this is obviously a question". Any change word, or any word we are unsure about, sends the message
to the LLM router, which still fails closed to ACTION. So a gap in the lists costs one extra model call, never a
change request passing as a plain question.
"""

import re
from collections.abc import Sequence

# ponytail: keyword lists, they miss new phrasings but only by routing to the LLM router. Grow them from
# the phrasing test set (tests/test_routing_rule.py); replace with a classifier only if the lists get unwieldy.
CHANGE_STEMS = (
    "restock", "reorder", "creat", "updat", "delet", "remov", "chang", "cancel", "launch", "schedul", "discount",
    "campaign", "promot", "pric", "send", "email", "messag", "notif", "increas", "decreas", "reduc", "markdown",
    "offer", "approv", "reject", "edit", "modif", "adjust", "publish", "activat", "pause", "appl", "rais", "lower",
    "boost",
)
CHANGE_WORDS = (  # whole words only: short, or common inside question words ("does", "orders", "market")
    "don", "dont", "go", "set", "add", "put", "run", "make", "order", "mark", "cut", "fix", "stop", "start",
    "place", "yes", "yeah", "yep", "ok", "okay", "sure", "please", "proceed", "confirm", "no", "nope",
)
CHANGE_PHRASES = (r"do\s+(?:that|it|this|so)",)  # "do" alone is also "what can you do?"
_CHANGE = re.compile(
    rf"\b(?:(?:{'|'.join(CHANGE_STEMS)})\w*|(?:{'|'.join(CHANGE_WORDS)})|(?:{'|'.join(CHANGE_PHRASES)}))\b",
    re.IGNORECASE,
)


def is_obviously_question(message: str, history: Sequence[dict] = ()) -> bool:
    """No change word in the message, and none in the assistant's last reply either: a follow-up like "the category
    is jeans" has no change word itself but continues an action the assistant just talked about."""
    last_reply = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")
    return _CHANGE.search(message) is None and _CHANGE.search(last_reply) is None
