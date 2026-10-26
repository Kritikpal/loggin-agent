# Conversation Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Follow-up messages in one AI chat conversation share context, and a clear yes/no in chat approves or rejects the pending campaign proposal.

**Architecture:** One new table, `chat_messages`, holds each conversation's text. Each `/ai/chat` turn loads the last 10 messages, passes them through the existing LangGraph workflow (agent, router, planner), and saves the user message plus the reply in one transaction after the turn succeeds. The pending proposal is derived from the newest message's `thread_id` and the checkpointer's own "waiting" state; a new AI approval check sends APPROVE/REJECT through the existing `resume_operator`.

**Tech Stack:** FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, LangChain/LangGraph (PostgreSQL checkpointer), Groq, pytest + pytest-asyncio, httpx.

**Spec:** `docs/superpowers/specs/2026-10-23-conversation-memory-design.md`

## Global Constraints

- **Do not commit.** The user asked to hold all commits; there are no commit steps.
- Run everything with `.\.venv\Scripts\python.exe` (Windows); tests: `.\.venv\Scripts\python.exe -m pytest`.
- `MAX_HISTORY_MESSAGES = 10`.
- Roles are exactly `'user'` and `'assistant'`.
- A turn is saved only after it succeeds; any failure (503, 409, 404) saves nothing.
- APPROVE/REJECT from chat go only through `resume_operator(model, checkpointer, thread_id, "APPROVED" | "REJECTED")`.
- Approval check: unusable output or a failed call is `"OTHER"`.
- Structured-output calls use the existing `_structured` helper (`method="json_schema"`).
- Spec deviation (intended): the turn flow lives in `app/ai/conversation.py`, not `app/services/`, because services depend only on an `AsyncSession` (README) and this flow needs the model and checkpointer.

## Review Focus

1. A correction after `INVALID_ACTION` ("the category is jeans") must reach the planner with the earlier failure in its input. Test: Task 3.
2. A pending thread whose checkpoints are gone must make "yes" a normal message, not a 500. Test: Task 4.
3. A chat approval whose campaign creation fails must save nothing and stay approvable. Test: Task 4.
4. After a chat "no", a later "yes" must be a normal message, not a second decision. Test: Task 4.
5. A workflow with no conversation (started outside `/ai/chat`) must still resume via the button without touching `chat_messages`. Test: Task 5.

---

## File map

| File | Change | Responsibility |
|---|---|---|
| `alembic/versions/0003_chat_messages.py` | create | the table |
| `app/models/chat_message.py` | create | `ChatMessage` ORM model |
| `app/models/__init__.py` | modify | export `ChatMessage` |
| `app/repositories/chat_message.py` | create | queries on `chat_messages` |
| `app/ai/agents/business_agent.py` | modify | `chat(..., history)`, prompt rule |
| `app/ai/graphs/operator_graph.py` | modify | `history` in state, `with_history`, `run_operator(..., history)`, `ApprovalVerdict`, `classify_approval`, `pending_proposal` |
| `app/ai/conversation.py` | create | `chat_turn`, `stored_reply`, `record_button_decision` |
| `app/schemas/ai.py` | modify | `conversation_id` on `ChatRequest`/`ChatResponse` |
| `app/api/v1/ai.py` | modify | routes call `chat_turn` / `record_button_decision` |
| `tests/scripted_model.py` | create | `ScriptedModel` and script helpers moved out of `test_ai_chat.py` |
| `tests/conftest.py` | modify | `use_model` fixture moved here; `chat_messages` in `TABLES` |
| `tests/test_chat_messages.py` | create | repository tests |
| `tests/test_ai_chat.py` | modify | import helpers; history tests |
| `tests/test_ai_conversation.py` | create | conversation and chat-approval tests |
| `README.md` | modify | document conversations |

---

### Task 1: `chat_messages` table, model and repository

**Files:**
- Create: `alembic/versions/0003_chat_messages.py`, `app/models/chat_message.py`, `app/repositories/chat_message.py`, `tests/test_chat_messages.py`
- Modify: `app/models/__init__.py`, `tests/conftest.py` (`TABLES`)

**Interfaces:**
- Produces:
  - `ChatMessage` (table `chat_messages`): `id: UuidPk`, `conversation_id: uuid.UUID` (NOT NULL), `role: str` (`String(20)`), `content: str` (`Text`), `thread_id: uuid.UUID | None` (`index=True`), `created_at: CreatedAt`. `__table_args__`: `CheckConstraint("role IN ('user', 'assistant')", name="role_allowed")`, `Index("ix_chat_messages_conversation_id_created_at", "conversation_id", "created_at")`.
  - `ChatMessageRepository(session: AsyncSession)` with:
    - `async recent(conversation_id: uuid.UUID, limit: int) -> list[ChatMessage]`: the newest `limit` messages, returned oldest first.
    - `async latest_thread_id(conversation_id: uuid.UUID) -> uuid.UUID | None`: `thread_id` of the newest message whose `thread_id` is not null.
    - `async conversation_for_thread(thread_id: uuid.UUID) -> uuid.UUID | None`
    - `add(*messages: ChatMessage) -> None`: `session.add_all`; the caller commits.

- [ ] **Step 1: Write the failing tests** in `tests/test_chat_messages.py` (use the `session` fixture; build rows with explicit `created_at` values one second apart, `t(i)`):

```python
async def test_recent_returns_the_newest_n_oldest_first(session):
    conv = uuid.uuid4()
    repo = ChatMessageRepository(session)
    repo.add(*(ChatMessage(conversation_id=conv, role="user", content=f"m{i}", created_at=t(i)) for i in range(12)))
    repo.add(ChatMessage(conversation_id=uuid.uuid4(), role="user", content="other", created_at=t(99)))
    await session.commit()
    assert [m.content for m in await repo.recent(conv, 10)] == [f"m{i}" for i in range(2, 12)]

async def test_user_sorts_before_assistant_at_the_same_instant(session):
    # both messages of one turn can share a timestamp
    ...  # user "q" and assistant "a" with identical created_at
    assert [m.role for m in await repo.recent(conv, 10)] == ["user", "assistant"]

async def test_latest_thread_id_skips_messages_without_one(session):
    # messages: thread A at t(0), thread B at t(1), plain at t(2)
    assert await repo.latest_thread_id(conv) == b
    assert await repo.latest_thread_id(uuid.uuid4()) is None

async def test_conversation_for_thread(session):
    assert await repo.conversation_for_thread(a) == conv
    assert await repo.conversation_for_thread(uuid.uuid4()) is None

async def test_database_rejects_unknown_roles(session):
    session.add(ChatMessage(conversation_id=uuid.uuid4(), role="system", content="x"))
    with pytest.raises(IntegrityError):
        await session.commit()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_chat_messages.py -v`
Expected: collection error, `ModuleNotFoundError: app.models.chat_message`.

- [ ] **Step 3: Implement the model, migration and repository.**
  - The model is as in Interfaces; add it to `app/models/__init__.py` and `__all__`.
  - Migration `0003`, `down_revision = '0002'`: `op.create_table('chat_messages', ...)` with constraint names `pk_chat_messages` and `ck_chat_messages_role_allowed`, plus indexes `ix_chat_messages_conversation_id_created_at` and `ix_chat_messages_thread_id`. `downgrade` drops the table.
  - Order in `recent`: `order_by(ChatMessage.created_at.desc(), ChatMessage.role.asc()).limit(limit)`, then reverse the list. On a timestamp tie, `'assistant'` comes first in the descending list, so `'user'` comes first after the reverse.
  - Add `chat_messages` to `TABLES` in `tests/conftest.py`.

- [ ] **Step 4: Run to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_chat_messages.py tests/test_schema.py -v`
Expected: all PASS (the session fixture migrates the test DB to head, which runs `0003`).

---

### Task 2: History through the workflow (agent, router, planner)

**Files:**
- Modify: `app/ai/agents/business_agent.py`, `app/ai/graphs/operator_graph.py`, `tests/test_ai_chat.py`

**Interfaces:**
- Produces:
  - `business_agent.chat(model, message: str, today: date | None = None, history: list[dict] | None = None) -> ChatResponse`. Each history item is `{"role": "user" | "assistant", "content": str}` and becomes a `HumanMessage` or `AIMessage`, placed before the new `HumanMessage(message)`.
  - `operator_graph.with_history(history: list[dict], text: str) -> str`: returns `text` unchanged when `history` is empty; otherwise `"Conversation so far:\n" + "\n".join(f"{role}: {content}") + "\n\n" + text`.
  - `OperatorState.history: NotRequired[list[dict]]`
  - `operator_graph.run_operator(model, checkpointer, message: str, history: list[dict] | None = None) -> ChatResponse`: the initial state is `{"message": message, "history": history or []}`.

- [ ] **Step 1: Write the failing tests** in `tests/test_ai_chat.py`:

```python
HISTORY = [{"role": "user", "content": "Sales in September 2026?"}, {"role": "assistant", "content": "$59,205."}]

async def test_agent_sees_history_before_the_new_message():
    model = ScriptedModel(script=[AIMessage("Jeans: $26,684.")], seen=[])
    await business_agent.chat(model, "And for jeans only?", history=HISTORY)
    turns = [(type(m).__name__, m.text) for m in model.seen[0] if not isinstance(m, SystemMessage)]
    assert turns == [("HumanMessage", "Sales in September 2026?"), ("AIMessage", "$59,205."),
                     ("HumanMessage", "And for jeans only?")]

async def test_router_and_planner_see_the_history(make_product, checkpointer):
    await make_product(category="Jeans")
    model = ScriptedModel(script=[AIMessage("ok"), route("ACTION"), plan()], seen=[])
    await operator_graph.run_operator(model, checkpointer, "Run 20% on that category", HISTORY)
    for call_input in (model.seen[1], model.seen[2]):  # router, planner
        text = "\n".join(m.text for m in call_input)
        assert "Conversation so far:\nuser: Sales in September 2026?\nassistant: $59,205." in text

def test_with_history_leaves_text_unchanged_without_history():
    assert operator_graph.with_history([], "Hi") == "Hi"
```

  Extend `test_system_prompt_keeps_grounding_and_evidence_rules` with the needles `"Earlier messages in the conversation are context"` and `"Call tools again for every number"`.

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ai_chat.py -k "history or prompt" -v`
Expected: FAIL (`chat()` has no `history` parameter; `with_history` is not defined).

- [ ] **Step 3: Implement.**
  - In `chat()`, build the messages from `history`.
  - Append these exact lines to the `Rules:` block of `SYSTEM_PROMPT`:

```
- Earlier messages in the conversation are context: use them to resolve references such as "that category",
  "the same period" or "jeans only".
- Call tools again for every number in your new answer. You may quote earlier numbers, never invent them.
```

  - In `operator_graph`:
    - `agent_node` passes `history=state.get("history", [])`.
    - `decision_node` uses `with_history(history, state["message"])` as the human text.
    - `planner_node` wraps its existing request text with `with_history`.
    - `run_operator` accepts and stores `history`.

- [ ] **Step 4: Run the whole AI chat file**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ai_chat.py -v`
Expected: all PASS. The existing router test still sees exactly `["How many did we sell?"]`, because there is no history.

---

### Task 3: Conversations on `/ai/chat` (no chat approval yet)

**Files:**
- Create: `app/ai/conversation.py`, `tests/scripted_model.py`, `tests/test_ai_conversation.py`
- Modify: `app/schemas/ai.py`, `app/api/v1/ai.py`, `tests/conftest.py`, `tests/test_ai_chat.py`

**Interfaces:**
- Consumes: `ChatMessageRepository`, `ChatMessage` (Task 1); `run_operator(..., history)` (Task 2).
- Produces:
  - `ChatRequest.conversation_id: uuid.UUID | None = None`
  - `ChatResponse.conversation_id: uuid.UUID | None = None`
  - `app/ai/conversation.py`:
    - `MAX_HISTORY_MESSAGES = 10`
    - `stored_reply(response: ChatResponse) -> str`: `response.response`, plus a second line by status:
      - `WAITING_APPROVAL` adds `f"Proposed campaign: {p.name} | {p.category} | {p.discount_percent}% | message: {p.message}"`.
      - `EXECUTED` adds `f"Created campaign: {c.name} (id {c.id})"`.
    - `async chat_turn(session: AsyncSession, model: BaseChatModel, checkpointer: BaseCheckpointSaver, message: str, conversation_id: uuid.UUID | None) -> ChatResponse`
  - `tests/scripted_model.py` exports `ScriptedModel`, `call`, `stock_call`, `route`, `plan`, `CAMPAIGN`. These are moved unchanged from `test_ai_chat.py`, which now imports them with `from tests.scripted_model import ...`.
  - The `use_model` fixture moves from `test_ai_chat.py` into `tests/conftest.py`.

- [ ] **Step 1: Move the scripted-model helpers and the `use_model` fixture.** Then run `.\.venv\Scripts\python.exe -m pytest tests/test_ai_chat.py -q`. Expected: all PASS, with the same count as before.

- [ ] **Step 2: Write the failing tests** in `tests/test_ai_conversation.py`:
  - Set `pytestmark = pytest.mark.usefixtures("tool_session", "checkpointer")`.
  - Add a helper `rows(session, conv)` that returns `(role, content, thread_id)` tuples via `ChatMessageRepository.recent(conv, 100)`.

```python
async def test_new_conversation_stores_the_turn(client, use_model, session):
    use_model(AIMessage("Hello!"), route("ANSWER"))
    body = (await client.post(URL, json={"message": "Hi"})).json()
    conv = uuid.UUID(body["conversation_id"])
    assert await rows(session, conv) == [("user", "Hi", None), ("assistant", "Hello!", None)]

async def test_follow_ups_see_earlier_turns(client, use_model, make_product):
    # turn 1 ANSWER; turn 2 ANSWER with conversation_id; turn 3 ACTION + plan(discount_percent=15)
    # assert: turn-2 agent input contains turn-1 user message and reply as Human/AI messages;
    # turn-3 planner input contains "Conversation so far:" and the turn-2 reply;
    # turn-3 status WAITING_APPROVAL; all three conversation_ids equal

async def test_only_the_last_10_messages_reach_the_model(client, use_model, session):
    # insert 12 alternating messages m0..m11 (explicit created_at), then post "next"
    # the agent's non-system input is m2..m11 followed by "next"

async def test_waiting_turn_stores_the_proposal_and_its_thread(client, use_model, make_product, session):
    # start a CREATE_CAMPAIGN turn; the stored assistant row has thread_id == body["thread_id"] and content containing
    # "Proposed campaign: Summer Jeans Sale | Jeans | 20.00% | message: 20% off all jeans this week."

async def test_correction_after_invalid_action_reaches_the_planner(client, use_model, make_product):  # Review Focus 1
    # turn 1: plan(category=None) -> INVALID_ACTION; turn 2 "The category is jeans": ACTION, plan()
    # turn-2 planner input contains "Missing: category"; turn-2 status WAITING_APPROVAL

async def test_unknown_conversation_is_404_and_saves_nothing(client, use_model, session):
    # 404 code "not_found"; no rows written

async def test_malformed_conversation_id_is_422(client, use_model): ...

async def test_failed_turn_saves_nothing(client, use_model, session):
    # existing conversation with 2 rows; use_model() (empty script) -> 503; still 2 rows
```

- [ ] **Step 3: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ai_conversation.py -v`
Expected: FAIL (`conversation_id` is rejected as an extra field, or missing from the response).

- [ ] **Step 4: Implement `chat_turn`**:
  1. If `conversation_id` is given, load `recent(conversation_id, MAX_HISTORY_MESSAGES)`. If that is empty, raise `NotFoundError(f"Conversation {conversation_id} not found")`. If no ID is given, use `uuid.uuid4()` and an empty history.
  2. Call `await session.commit()` to end the read transaction, so no connection is held during model calls.
  3. `response = await run_operator(model, checkpointer, message, history)`
  4. `add` the user `ChatMessage` and the assistant `ChatMessage(content=stored_reply(response), thread_id=...)`, then `commit`. `ChatResponse.thread_id` is a `str | None`, so store `uuid.UUID(response.thread_id) if response.thread_id else None`.
  5. Return `response.model_copy(update={"conversation_id": conversation_id})`.

  In `app/api/v1/ai.py`, `ai_chat` gains `session: SessionDep` (from `app.api.deps`) and calls `chat_turn`.

- [ ] **Step 5: Run to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ai_conversation.py tests/test_ai_chat.py -v`
Expected: all PASS.

---

### Task 4: Approve or reject from chat

**Files:**
- Modify: `app/ai/graphs/operator_graph.py`, `app/ai/conversation.py`, `tests/scripted_model.py`, `tests/test_ai_conversation.py`

**Interfaces:**
- Consumes: `latest_thread_id` (Task 1), `with_history` (Task 2), `chat_turn` (Task 3), the existing `resume_operator`, `_is_waiting`, `_structured`.
- Produces:
  - `class ApprovalVerdict(BaseModel)`: `decision: Literal["APPROVE", "REJECT", "OTHER"]`, with description "APPROVE or REJECT only when the message clearly decides the pending proposal; otherwise OTHER".
  - `async classify_approval(model, proposal: dict, history: list[dict], message: str) -> Literal["APPROVE", "REJECT", "OTHER"]`. It calls `_structured(model, ApprovalVerdict, APPROVAL_PROMPT, with_history(history, f"Pending proposal:\n{json.dumps(proposal)}\n\nNew message:\n{message}"))`. A `None` result gives `"OTHER"`.
  - `async pending_proposal(checkpointer, thread_id: str) -> dict | None`. It reads `build_graph(None, checkpointer).aget_state(...)` and returns `snapshot.values["proposal"]` when `_is_waiting(snapshot)`, otherwise `None`. A storage error is raised as `AIUnavailableError("The workflow could not be loaded. Try again shortly.")`.
  - `tests/scripted_model.verdict(value: str) -> AIMessage`, i.e. `call({"decision": value}, "v1", tool="ApprovalVerdict")`.
  - `APPROVAL_PROMPT` (exact copy):

```
Decide whether the user's new message approves or rejects the pending proposal.

APPROVE: the message clearly says yes to this proposal (e.g. "yes", "go ahead", "approve it").
REJECT: the message clearly says no to this proposal (e.g. "no", "cancel it", "don't").
OTHER: anything else - questions, requests to change the proposal ("make it 10%"), new requests, or anything unclear.
```

- [ ] **Step 1: Write the failing tests** in `tests/test_ai_conversation.py`. A helper `waiting(client, use_model, make_product)` runs a CREATE_CAMPAIGN turn and returns the body; `say(client, conv, text)` posts with the `conversation_id`.

```python
async def test_yes_executes_the_pending_campaign(client, use_model, make_product, session):
    body = await waiting(client, use_model, make_product)
    model = use_model(verdict("APPROVE"))
    res = (await say(client, body["conversation_id"], "yes go ahead")).json()
    assert res["status"] == "EXECUTED" and res["campaign"]["id"] == body["thread_id"]
    assert await campaign_count(session) == 1
    assert len(model.seen) == 1  # only the approval check ran
    # the stored assistant row contains "Created campaign: Summer Jeans Sale (id <thread_id>)"

async def test_no_rejects_it(...): ...                       # REJECTED, 0 campaigns
async def test_change_request_is_other_and_stays_pending(...):
    # verdict("OTHER"), AIMessage("..."), route("ANSWER") -> COMPLETED; then verdict("APPROVE") -> EXECUTED
async def test_unusable_verdict_is_other(...):
    # AIMessage("not structured"), AIMessage("..."), route("ANSWER") -> COMPLETED, 0 campaigns
async def test_no_approval_check_without_a_pending_proposal(client, use_model):
    # new conversation, "yes": script AIMessage("Yes to what?"), route("ANSWER") is consumed exactly (len(seen) == 2)
async def test_newest_proposal_is_the_one_approved(...):
    # two waiting turns in one conversation; "yes" executes the second thread;
    # the first thread is still approvable via RESUME (200, EXECUTED)
async def test_missing_checkpoint_makes_yes_a_normal_message(..., checkpointer):    # Review Focus 2
    # delete the thread's rows from checkpoints/checkpoint_blobs/checkpoint_writes; "yes" -> COMPLETED via agent
async def test_failed_chat_approval_saves_nothing_and_can_be_retried(..., monkeypatch):  # Review Focus 3
    # execute_proposal raises -> 503, row count unchanged; then verdict("APPROVE") -> EXECUTED, 1 campaign
async def test_yes_after_no_is_a_normal_message(...):         # Review Focus 4
```

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ai_conversation.py -k "yes or no_ or verdict or pending or proposal or approval" -v`
Expected: FAIL. "yes" goes to the agent, which consumes `verdict(...)` as an agent turn.

- [ ] **Step 3: Implement.** In `chat_turn`, after loading the history and only when `conversation_id` was given:
  1. `thread = await repo.latest_thread_id(conversation_id)`
  2. `proposal = await pending_proposal(checkpointer, str(thread)) if thread else None`
  3. If there is a proposal and `classify_approval(...)` returns APPROVE or REJECT, `response = await resume_operator(model, checkpointer, str(thread), "APPROVED" | "REJECTED")`. Otherwise call `run_operator` as before.
  4. Saving the turn is unchanged. `response.thread_id` is set for EXECUTED/REJECTED, so the decision row carries the thread.

- [ ] **Step 4: Run to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ai_conversation.py tests/test_ai_chat.py -v`
Expected: all PASS.

---

### Task 5: Button decisions update the conversation; README

**Files:**
- Modify: `app/ai/conversation.py`, `app/api/v1/ai.py`, `tests/test_ai_conversation.py`, `README.md`

**Interfaces:**
- Consumes: `conversation_for_thread` (Task 1).
- Produces: `async record_button_decision(session: AsyncSession, thread_id: uuid.UUID, response: ChatResponse) -> uuid.UUID | None`. It finds the conversation; if there is none, it returns `None` and writes nothing. Otherwise it adds one assistant `ChatMessage` with `thread_id` and this exact content, commits, and returns the conversation ID:
  - `EXECUTED`: `f"Approved with the approval button. Campaign '{c.name}' was created (id {c.id})."`
  - `REJECTED`: `"Rejected with the approval button. Nothing was changed."`

- [ ] **Step 1: Write the failing tests**:

```python
async def test_button_approval_is_recorded_and_yes_is_then_normal(client, use_model, make_product, session):
    body = await waiting(client, use_model, make_product)
    res = (await client.post(RESUME.format(body["thread_id"]), json={"decision": "APPROVED"})).json()
    assert res["conversation_id"] == body["conversation_id"]
    # last stored row: ("assistant", "Approved with the approval button. Campaign 'Summer Jeans Sale' was created (id <thread>).", thread)
    model = use_model(AIMessage("It's already approved."), route("ANSWER"))
    assert (await say(client, body["conversation_id"], "yes")).json()["status"] == "COMPLETED"
    assert len(model.seen) == 2  # no approval check

async def test_button_rejection_is_recorded(...): ...

async def test_workflow_without_a_conversation_still_resumes(client, use_model, make_product, checkpointer, session):  # Review Focus 5
    # run_operator(...) directly (no chat_turn), then RESUME APPROVED -> 200, conversation_id None, chat_messages empty
```

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ai_conversation.py -k button -v`
Expected: FAIL (no outcome row; `conversation_id` is null).

- [ ] **Step 3: Implement.** `resume_workflow` gains `session: SessionDep`. After `resume_operator`, it calls `record_button_decision` and returns `response.model_copy(update={"conversation_id": ...})`. Update its docstring to say the conversation gets the outcome.

- [ ] **Step 4: Update `README.md`, "AI chat and human approval".** Describe:
  - `conversation_id` (omit it to start a conversation, send it back to continue);
  - that the last 10 messages are used;
  - approving with a clear yes/no in chat, decided by the AI, with anything unclear treated as a normal message;
  - that the button still works and records its outcome in the conversation;
  - the `chat_messages` table.

- [ ] **Step 5: Run the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: all PASS (160 existing + new).

- [ ] **Step 6: Apply the migration and smoke-test the live server (needs `GROQ_API_KEY`).**
  1. Run `.\.venv\Scripts\python.exe -m alembic upgrade head`.
  2. Send the spec's three demo messages in one conversation, then "yes".
  3. Expected: #2 reports jeans for September 2026, #3 is `WAITING_APPROVAL` with category Jeans at 15.00, and "yes" is `EXECUTED`.
  4. Delete that smoke-test campaign afterwards only if the user asks.
