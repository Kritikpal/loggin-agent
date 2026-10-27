# TODO: SECURITY — run llm-secure-patterns to apply LLM security patterns
"""Semantic memory, the model-free half: what the extraction call may return, the gates a suggestion must pass,
and what to do with the ones that pass. See docs/superpowers/specs/2026-10-26-semantic-memory-design.md.

The model only proposes `Candidate`s. Nothing here calls it, and nothing here writes to the database."""

import json
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import md5
from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.guardrails import EMAIL, NUMBER, PHONE
from app.ai.llm import structured_call
from app.ai.prompts import conversation, load_prompt, untrusted
from app.models import Memory
from app.schemas.ai import MemoryConflict
from app.repositories.memory import LIVE, MemoryRepository

logger = logging.getLogger(__name__)

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
CONTENT_MIN, CONTENT_MAX, SUBJECT_MAX = 10, 300, 60
BUSINESS_WORDS = frozenset({"sale", "sales", "stock", "revenue", "unit", "units", "price", "prices", "sold"})
BUSINESS_WORD_DISTANCE = 3  # tokens between a sales word and a number that still count as "next to"
MIN_PHONE_DIGITS = 9  # a dated "2026-10-26" has 8 digits and must not look like a phone number
FORBIDDEN_IN_PREFERENCES = ("approv", "without asking")
MIN_MESSAGE_CHARS = 15  # "yes", "thanks" and "and jeans?" are not worth a model call
WINDOW_MESSAGES = 4  # chat messages shown to the extractor, so "make *that* my default" can be resolved
TOP_OBSERVATIONS = 10  # best text matches shown to the extractor, so it can link an update to the memory it changes
# ponytail: plus the newest live observations up to this cap, so a reworded correction still sees the note it corrects.
# Past ~30 observations an older note with no shared word can be missed again; trigram or pgvector retrieval then.
EXTRACTOR_OBSERVATIONS = 30
RECALL_OBSERVATIONS = 5  # shown to the agent and planner
SHOWN_CONFLICTS = 3  # open conflicts sent with each chat reply, so an ignored one does not crowd the chat
SIMILARITY_FLOOR = 0.8
MAX_RETRIES = 3  # extra attempts after a 429; Groq's per-minute token limit frees up while we wait
RETRY_WAIT_SECONDS = 20
SKIPPED_STATUSES = ("EXECUTED", "REJECTED", "BLOCKED")  # approval decisions and blocked turns carry no new facts
# Words almost every message in this domain contains; searching for them would pull in unrelated notes.
RECALL_IGNORE = frozenset({"sale", "sales", "store", "shop", "week", "month", "today", "report", "show", "give"})
TOKEN = re.compile(r"[a-z]+|\d[\d,]*(?:\.\d+)?")


class Candidate(BaseModel):
    """One possible memory the model found. Field descriptions are part of the extraction prompt."""

    kind: Literal["preference", "observation"]
    updates_id: str | None = Field(description="Id of the existing memory this changes; null for a new memory.")
    key: str | None = Field(description="Preference only: snake_case. REUSE the existing key for the same preference.")
    value: str | None = Field(description="Preference only: 'Monday to Sunday', '15%', 'short bullet lists'.")
    subject: str | None = Field(description="Observation only: a category name, or 'store', 'suppliers', ...")
    stance: Literal["new", "confirms", "refines", "contradicts"] = Field(
        description="Against the memory it updates; 'new' for a memory that updates nothing."
    )
    content: str = Field(description="One self-contained sentence.")
    evidence: str = Field(description="The manager's exact words from their latest message.")


class Extraction(BaseModel):
    """Lasting memories found in the manager's latest message."""

    memories: list[Candidate] = Field(description="Empty when nothing qualifies.")


@dataclass(frozen=True)
class Turn:
    """What happened in the turn the candidate came from."""

    user_messages: tuple[str, ...]  # the manager's messages in the window sent to the model; the last is the latest
    assistant_reply: str
    tool_results: str


@dataclass(frozen=True)
class Known:
    """What the database says about a candidate. `remember` fills it in, which keeps the gates pure."""

    existing: dict[str, Memory]  # the memories sent to the model, by id
    active_pref: Memory | None  # the active preference with the candidate's key, if any
    hash_taken: bool  # a live memory, other than `flagged`, has the candidate's content hash
    similar: bool  # trigram similarity >= 0.8 with a live memory of the same kind
    flagged: Memory | None = None  # a flagged correction the manager now states again: this candidate settles it


class Decision(StrEnum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    SKIP = "SKIP"
    FLAG = "FLAG"
    RESOLVE = "RESOLVE"  # settle a flagged conflict in favour of the manager's latest statement


def _normalise(text: str) -> str:
    text = " ".join(text.lower().split())
    return text.strip("\"'“”‘’ ").rstrip(".").strip()


def content_hash(text: str) -> str:
    return md5(_normalise(text).encode(), usedforsecurity=False).hexdigest()


def _numbers(text: str) -> set[str]:
    """Numbers as comparable strings: "26,684" and "26684.00" are the same figure."""
    return {re.sub(r"\.0+$", "", n.replace(",", "")) for n in NUMBER.findall(text)}


def _candidate_numbers(c: Candidate) -> set[str]:
    return _numbers(c.content) | _numbers(c.value or "")


def origin(c: Candidate, turn: Turn) -> Literal["stated", "endorsed"]:
    """"endorsed" when a number comes from the assistant's reply alone: the manager agreed to a suggestion."""
    from_reply_only = (_candidate_numbers(c) & _numbers(turn.assistant_reply)) - _numbers(" ".join(turn.user_messages))
    return "endorsed" if from_reply_only else "stated"


# --- gates: each returns True when the candidate passes; the function name is the SKIP reason -----------------------

def schema(c: Candidate, turn: Turn, known: Known) -> bool:
    """Well-formed, and it only updates a memory we sent, of the same kind."""
    if not CONTENT_MIN <= len(c.content) <= CONTENT_MAX or not c.evidence.strip():
        return False
    if c.kind == "preference":
        if not (c.key and KEY_PATTERN.fullmatch(c.key) and c.value and c.value.strip()):
            return False
    elif not (c.subject and c.subject.strip() and len(c.subject) <= SUBJECT_MAX):
        return False
    if c.updates_id is None:
        return True
    target = known.existing.get(c.updates_id)
    return target is not None and target.kind == c.kind


def not_said(c: Candidate, turn: Turn, known: Known) -> bool:
    """The manager said it: the evidence is in their latest message, and no number comes from tool results alone."""
    evidence = _normalise(c.evidence)
    if not evidence or evidence not in _normalise(turn.user_messages[-1]):
        return False
    heard = _numbers(" ".join(turn.user_messages)) | _numbers(turn.assistant_reply)
    return _candidate_numbers(c) <= heard


def business_data(c: Candidate, turn: Turn, known: Known) -> bool:
    """Observations only: not a copy of sales, stock or price data the database already holds."""
    if c.kind != "observation":
        return True
    if _numbers(c.content) & (_numbers(turn.tool_results) | _numbers(turn.assistant_reply)):
        return False
    tokens = TOKEN.findall(c.content.lower())
    words = [i for i, t in enumerate(tokens) if t in BUSINESS_WORDS]
    figures = [i for i, t in enumerate(tokens) if t[0].isdigit()]
    return not any(abs(w - n) <= BUSINESS_WORD_DISTANCE for w in words for n in figures)


def pii(c: Candidate, turn: Turn, known: Known) -> bool:
    """No email address or phone number in what would be stored."""
    for text in (c.content, c.evidence):
        if EMAIL.search(text):
            return False
        if any(sum(ch.isdigit() for ch in m.group()) >= MIN_PHONE_DIGITS for m in PHONE.finditer(text)):
            return False
    return True


def duplicate(c: Candidate, turn: Turn, known: Known) -> bool:
    """Nothing new: not the same text, not the same preference value, not a near copy of a live memory.
    Updates and settled flags skip the near-copy test: they are meant to look like the row they replace."""
    if known.hash_taken:
        return False
    active = known.active_pref
    if c.kind == "preference" and active is not None:
        if _normalise(str(active.attributes.get("value", ""))) == _normalise(c.value or ""):
            return False
    return not (c.updates_id is None and known.flagged is None and known.similar)


def safety_rule(c: Candidate, turn: Turn, known: Known) -> bool:
    """A preference may not touch the approval workflow."""
    if c.kind != "preference":
        return True
    text = f"{c.key} {c.value} {c.content}".lower()
    return not any(phrase in text for phrase in FORBIDDEN_IN_PREFERENCES)


# `schema` must stay first: the others rely on the fields it checks.
GATES: tuple[Callable[[Candidate, Turn, Known], bool], ...] = (schema, not_said, business_data, pii, duplicate, safety_rule)


def first_failure(c: Candidate, turn: Turn, known: Known) -> str | None:
    """The name of the first gate the candidate fails (the SKIP reason), or None if it passes them all."""
    return next((gate.__name__ for gate in GATES if not gate(c, turn, known)), None)


def decide(c: Candidate, known: Known) -> Decision:
    """INSERT, UPDATE or FLAG for a candidate that passed every gate (spec 5.3). SKIP comes from `first_failure`."""
    is_update = c.updates_id is not None
    if known.flagged is not None:  # said twice: the manager means it, so the latest statement wins
        return Decision.RESOLVE
    if c.kind == "preference":  # the manager is the authority on their own preferences: the latest statement wins
        return Decision.UPDATE if is_update or known.active_pref is not None else Decision.INSERT
    if not is_update:
        return Decision.INSERT
    return Decision.FLAG if c.stance == "contradicts" else Decision.UPDATE


# --- remember: extract -> gates -> decide -> apply ---------------------------------------------------------------

EXTRACT_PROMPT = load_prompt("memory_extract")


def search_terms(user_messages: Sequence[str]) -> str:
    """Words of the last two user messages, minus everyday ones, for a full-text query ("jeans or july"). The last
    two, so a follow-up like "And what about July?" still finds the jeans note."""
    words = re.findall(r"[a-z]{3,}", " ".join(user_messages[-2:]).lower())
    return " or ".join(dict.fromkeys(w for w in words if w not in RECALL_IGNORE))


NOTES_HEADER = load_prompt("memory_notes_header")


def _note(m: Memory) -> str:
    agreed = " (agreed to a suggestion)" if m.attributes.get("origin") == "endorsed" else ""
    if m.kind == "preference":
        return f"- {m.key}: {m.content}{agreed}"
    return f"- [{m.subject}] {m.content}{agreed} (noted {m.created_at:%Y-%m-%d})"


NOTES_TAG = "UNTRUSTED_SAVED_NOTES"


def render_notes(preferences: Sequence[Memory], observations: Sequence[Memory]) -> str:
    """The notes block for the agent's system prompt and the planner's input; empty when nothing is saved."""
    # SECURITY: LLM01 (Prompt Injection) / LLM07 (System Prompt Leakage) — Level B (Moderate)
    # Pattern: system_prompt_template — saved notes are user-written and replayed on every later turn, so they sit
    # inside UNTRUSTED tags after the trusted header, with angle brackets defanged so a note cannot close the block.
    # Behavioural hint to the model, not a boundary. Applied by: llm-secure-patterns v1.0.0 / System Prompt Design,
    # 2026-10-27.
    sections = [("Preferences:", preferences), ("Observations:", observations)]
    body = "\n".join(line for title, rows in sections if rows for line in (title, *map(_note, rows)))
    return f"{NOTES_HEADER}\n{untrusted(NOTES_TAG, body)}" if body else ""


async def recall(
    session: AsyncSession, message: str, history: Sequence[dict], conversation_id: uuid.UUID | None = None
) -> str:
    """The saved notes this turn should be given: every active preference, plus the observations that match the
    last two user messages. Indexed queries only, no model call. A failure means "no notes", never a failed chat."""
    user_messages = [m["content"] for m in history if m["role"] == "user"] + [message]
    terms = search_terms(user_messages)
    try:
        repo = MemoryRepository(session)
        preferences = await repo.active_preferences()
        observations = await repo.related_observations(terms, RECALL_OBSERVATIONS) if terms else []
    except Exception:
        logger.exception("Memory recall failed for conversation %s", conversation_id)
        await session.rollback()
        return ""
    ids = ",".join(str(m.id) for m in [*preferences, *observations])
    logger.info('memory recall conversation=%s preferences=%d observations=%d ids=%s terms="%s"',
                conversation_id, len(preferences), len(observations), ids, terms)
    return render_notes(preferences, observations)


async def open_conflicts(session: AsyncSession, conversation_id: uuid.UUID | None = None) -> list[MemoryConflict]:
    """Flagged corrections waiting for the manager, for the chat to show inline. Like recall, a failure means
    "none", never a failed chat."""
    try:
        rows = await MemoryRepository(session).open_conflicts(SHOWN_CONFLICTS)
    except Exception:
        logger.exception("Memory conflict lookup failed for conversation %s", conversation_id)
        await session.rollback()
        return []
    return [
        MemoryConflict(
            id=flag.id, content=flag.content, evidence=flag.evidence, created_at=flag.created_at,
            earlier_id=old.id if old else None, earlier_content=old.content if old else None,
        )
        for flag, old in rows
    ]


async def extract(model: BaseChatModel, human: str) -> Extraction | None:
    """One strict structured-output call. None when it fails or returns nothing usable. A 429 is retried: this runs
    in the background, so waiting costs the manager nothing."""
    return await structured_call(
        model, Extraction, EXTRACT_PROMPT, human, strict=True, retries=MAX_RETRIES, retry_wait=RETRY_WAIT_SECONDS
    )


def _view(m: Memory) -> dict:
    who = {"key": m.key, "value": m.attributes.get("value")} if m.kind == "preference" else {"subject": m.subject}
    return {"id": str(m.id), "kind": m.kind, **who, "content": m.content, "status": m.status}


def _log(decision: Decision, c: Candidate, conversation_id: uuid.UUID, reason: str | None = None,
         replaces: uuid.UUID | None = None) -> None:
    parts = [f"memory decision={decision}", f"reason={reason}" if reason else "", f"kind={c.kind}",
             f"key={c.key}" if c.key else "", f"replaces={replaces}" if replaces else "",
             f'conversation={conversation_id} content="{c.content}"']
    logger.info(" ".join(p for p in parts if p))


async def _known(repo: MemoryRepository, c: Candidate, existing: dict[str, Memory], prefs: dict[str, Memory]) -> Known:
    target = existing.get(c.updates_id) if c.updates_id else None
    key = target.key if target is not None and target.kind == "preference" else c.key
    same_text = await repo.find_duplicate(content_hash(c.content))
    flagged = await _flag_settled(repo, c, target, same_text)
    return Known(
        existing=existing,
        active_pref=prefs.get(key) if c.kind == "preference" and key else None,
        hash_taken=same_text is not None and (flagged is None or same_text.id != flagged.id),
        similar=await repo.has_similar(c.content, c.kind, SIMILARITY_FLOOR),
        flagged=flagged,
    )


async def _flag_settled(repo: MemoryRepository, c: Candidate, target: Memory | None,
                        same_text: Memory | None) -> Memory | None:
    """The flagged correction this observation states again, if any: it updates the flag, repeats its exact text,
    or contradicts the same note a second time."""
    if c.kind != "observation":
        return None
    for row in (target, same_text):
        if row is not None and row.status == "conflicted":
            return row
    if target is not None and target.status == "active" and c.stance == "contradicts":
        return await repo.open_conflict(target.id)
    return None


async def _apply(
    repo: MemoryRepository, decision: Decision, c: Candidate, turn: Turn, known: Known, conversation_id: uuid.UUID
) -> uuid.UUID | None:
    """Write one decision; returns the id of the row it replaces or conflicts with, for the log."""
    target = known.existing.get(c.updates_id) if c.updates_id else known.active_pref
    if decision is Decision.RESOLVE:  # the flag and the note it contradicted both give way to the new row
        target = known.flagged
        if target.conflicts_with_id:
            await repo.supersede(target.conflicts_with_id)
    keeps_key = target is not None and target.kind == "preference" and decision is Decision.UPDATE
    row = Memory(
        kind=c.kind, key=target.key if keeps_key else c.key, subject=c.subject, content=c.content,
        attributes={**({"value": c.value} if c.value else {}), "stance": c.stance, "origin": origin(c, turn)},
        evidence=c.evidence, source_conversation_id=conversation_id, content_hash=content_hash(c.content),
    )
    if decision in (Decision.UPDATE, Decision.RESOLVE):
        await repo.supersede(target.id)  # first: the unique index allows one active row per key
        row.replaces_id = target.id
    elif decision is Decision.FLAG:
        row.status, row.conflicts_with_id = "conflicted", target.id
    repo.add(row)
    return target.id if target is not None and decision is not Decision.INSERT else None


async def remember(
    model: BaseChatModel,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    message: str,
    reply: str,
    status: str,
    tool_results: str,
    history: Sequence[dict],
    conversation_id: uuid.UUID,
) -> None:
    """Save what the manager said in this turn, if it is worth keeping. Best effort and off the request path: it
    never raises, so a failure here can only mean "remembers less". Takes plain values and its own sessions
    because it outlives the request that started it."""
    if status in SKIPPED_STATUSES or len(message.strip()) < MIN_MESSAGE_CHARS:
        return
    try:
        await _remember(model, session_factory, message, reply, tool_results, history, conversation_id)
    except Exception:
        logger.exception("Memory update failed for conversation %s", conversation_id)


async def _remember(model, session_factory, message, reply, tool_results, history, conversation_id) -> None:
    window = [*history, {"role": "user", "content": message}, {"role": "assistant", "content": reply}]
    window = window[-WINDOW_MESSAGES:]
    turn = Turn(
        user_messages=tuple(m["content"] for m in window if m["role"] == "user"),
        assistant_reply="\n".join(m["content"] for m in window if m["role"] == "assistant"),
        tool_results=tool_results,
    )
    terms = search_terms([m["content"] for m in [*history, {"role": "user", "content": message}] if m["role"] == "user"])
    async with session_factory() as session:  # closed before the model call: no connection is held while waiting
        repo = MemoryRepository(session)
        prefs = await repo.active_preferences()
        matches = await repo.related_observations(terms, TOP_OBSERVATIONS) if terms else []
        newest = await repo.list("observation", LIVE, EXTRACTOR_OBSERVATIONS)  # includes flagged corrections
    observations = list({m.id: m for m in [*matches, *newest]}.values())[:EXTRACTOR_OBSERVATIONS]
    existing = {str(m.id): m for m in [*prefs, *observations]}
    memories = untrusted("UNTRUSTED_EXISTING_MEMORIES", json.dumps([_view(m) for m in existing.values()]))
    human = f"{memories}\n\n{conversation(window)}"

    extraction = await extract(model, human)
    if extraction is None or not extraction.memories:
        return
    prefs_by_key = {m.key: m for m in prefs}
    applied: list[tuple[Decision, Candidate, uuid.UUID | None]] = []
    async with session_factory() as session:
        repo = MemoryRepository(session)
        try:
            for c in extraction.memories:
                known = await _known(repo, c, existing, prefs_by_key)
                if failure := first_failure(c, turn, known):
                    _log(Decision.SKIP, c, conversation_id, reason=failure)
                    continue
                decision = decide(c, known)
                applied.append((decision, c, await _apply(repo, decision, c, turn, known, conversation_id)))
            await session.commit()  # one transaction: a turn's decisions land together
        except IntegrityError:  # two extractions raced, or one turn produced two rows for one key
            await session.rollback()
            logger.warning("Memory batch rolled back by a unique index for conversation %s", conversation_id)
            return
    for decision, c, other in applied:
        _log(decision, c, conversation_id, replaces=other)
