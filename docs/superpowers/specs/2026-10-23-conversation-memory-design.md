# Conversation memory for the AI chat - design

Date: 2026-10-23
Status: approved in conversation, revised after an over-engineering audit, awaiting written-spec review
Scope: project 1 of 2. Project 2 (long-term memory across chats) gets its own spec after this ships.

## 1. Problem

Every `POST /api/v1/ai/chat` call starts from nothing:

- `ChatRequest` carries only `message`, so a client cannot say "this continues my earlier chat".
- `run_operator` creates a new `uuid4()` workflow thread per call.
- The agent sees only `[HumanMessage(message)]`; the router and planner see only the current text.
- `OperatorState.message` is a single string.

Observed on the live server:

| # | Message | Result |
|---|---|---|
| 1 | "What were total sales in September 2026?" | Correct summary for Sep 1-30, Jeans $26,684 |
| 2 | "And for jeans only?" | Asked for the date range again |
| 3 | "Run a 15% campaign on that category" | `INVALID_ACTION: Missing: category` |

## 2. Goals and non-goals

Goals

- Follow-up messages in the same conversation are understood by the agent, router and planner.
- A clear yes/no in the conversation approves or rejects the pending campaign proposal. The AI decides whether a
  message is a decision.
- The existing approval workflow (pause/resume, durability, no duplicate campaigns) is reused unchanged.

Non-goals (YAGNI)

- Long-term memory across conversations (project 2).
- Multiple users, logins, per-user conversations. There is one owner/operator.
- Reading, listing, renaming or deleting conversations through the API. Add `GET /ai/conversations/{id}` when a UI
  needs to redraw a chat.
- Summarising old history.
- Changing a proposal from chat ("yes but 10%" is not an approval; it is handled as a new message).
- A friendly reply for the rare race where the button and a chat "yes" decide the same proposal at once; the existing
  409 is returned.

Success criteria

- The three messages above, sent as one conversation, give: #2 answers jeans sales for September 2026; #3 produces a
  `WAITING_APPROVAL` proposal for category Jeans at 15%; a following "yes" gives `EXECUTED` and exactly one campaign.

## 3. API

### `POST /api/v1/ai/chat`

Request: `{"message": str, "conversation_id": UUID | null}` (`extra="forbid"` as today).

- `conversation_id` omitted or null: a new conversation id (`uuid4`) is generated.
- Given, but no messages exist for it: 404 `not_found`. Malformed: 422.

Response: today's `ChatResponse` plus `conversation_id: UUID` (always set). `status`, `thread_id`,
`proposed_action`, `campaign`, `tool_calls` keep their current meaning.

### `POST /api/v1/ai/workflows/{thread_id}/resume`

Unchanged for callers. After a successful decision it also appends one assistant message to the conversation that
holds this `thread_id` (if any), e.g. "Approved with the approval button. Campaign 'Summer Jeans Sale' was created."
so later turns know the outcome. Workflows started before this change have no conversation and are decided as today.

## 4. Data

One Alembic migration (`0003`), one table:

```
chat_messages
  id               uuid PK (gen_random_uuid())
  conversation_id  uuid NOT NULL          -- no conversations table: a conversation is its messages
  role             varchar(20) NOT NULL CHECK (role IN ('user', 'assistant'))
  content          text NOT NULL
  thread_id        uuid NULL              -- workflow that produced this reply (proposals and decisions)
  created_at       timestamptz NOT NULL default now()
  INDEX (conversation_id, created_at)
  INDEX (thread_id)
```

- Only text is stored. Raw tool output is not.
- For a `WAITING_APPROVAL` turn, the stored assistant content is the reply plus the proposal, e.g.
  "...need your approval. Proposed campaign: Summer Jeans Sale | Jeans | 15.00% | message: ...". The API response
  text is unchanged. Without this, later references to "that campaign" have nothing to resolve against.
- For `EXECUTED`, the stored content names the created campaign and its id.
- There is no "pending" column. Which proposal is pending is derived (section 5), so it can never disagree with the
  workflow's own checkpointed state.

Code follows the existing layering: `app/models/chat_message.py`, `app/repositories/chat_message.py`,
`app/services/conversation.py` (the turn flow below). `ChatRequest`/`ChatResponse` stay in `app/schemas/ai.py`.
`tests/conftest.py` adds `chat_messages` to the per-test `TRUNCATE`.

## 5. Turn flow

```
POST /ai/chat {message, conversation_id?}
  1. history = last MAX_HISTORY_MESSAGES (10) messages of the conversation, oldest first
     (conversation_id given but no messages -> 404)
  2. pending = the newest message in the conversation that has a thread_id, if its workflow is waiting
     for approval (_is_waiting on the checkpointed state)
  3. If pending:
       verdict = approval check (AI)            -> APPROVE / REJECT / OTHER
       APPROVE -> resume_operator(thread, "APPROVED")   -> EXECUTED
       REJECT  -> resume_operator(thread, "REJECTED")   -> REJECTED
       OTHER or unusable output -> step 4
  4. run_operator(message, history)  -> COMPLETED / WAITING_APPROVAL / INVALID_ACTION
  5. In ONE transaction: insert the user message and the assistant message (with thread_id when the turn
     produced or decided a proposal)
  6. Return the response with conversation_id
```

### Approval check

- Runs only when a proposal is pending, before the agent.
- Structured output `ApprovalVerdict { decision: "APPROVE" | "REJECT" | "OTHER" }`, `method="json_schema"`, through the
  existing `_structured` helper.
- Input: the pending proposal (from the workflow's checkpointed state, not from chat text), the history as text and
  the new message.
- Prompt: APPROVE or REJECT only when the message clearly decides this proposal; questions, changes ("make it 10%")
  and anything unclear are OTHER.
- Unusable output or a failed call is OTHER: the message is answered normally and the proposal stays pending.
- APPROVE/REJECT always go through `resume_operator`, so only the stored, validated proposal can be decided.

### Edge case

- New proposal while one is pending: the newest message with a `thread_id` now points to the new workflow, so "yes"
  applies to the latest. The older workflow stays waiting and can still be decided with the resume endpoint.

Everything else follows from step 2: a proposal decided with the button, or decided by a turn whose save failed, is
no longer waiting, so the next message is handled normally.

Known limit: two messages to the same conversation at the same moment are not serialised. Acceptable for one user.
If the button and a chat "yes" race, `resume_operator` lets one win and the other gets the existing 409.

## 6. What the model sees

| Step | Input |
|---|---|
| Agent | system prompt, history as `HumanMessage`/`AIMessage` turns, then the new `HumanMessage` |
| Router (`Decision`) | history as text, then the new message |
| Planner (`ActionPlan`) | history as text, the new message, this turn's read-only findings |
| Approval check | pending proposal, history as text, new message |

- `OperatorState` gains `history: list[dict]` (`{"role", "content"}`), plain JSON so it checkpoints as today.
- `chat()` in `business_agent.py` takes `history` and prepends it to the messages. One helper renders history as text
  for the three structured-output calls.
- New system-prompt rule: use earlier messages to resolve references ("that category", "same period", "jeans
  only"), but call tools again for any number in the new answer; earlier numbers may be quoted, never invented.
- `MAX_HISTORY_MESSAGES = 10` is a named constant. No per-message cap: user messages are capped at 2000 characters
  and assistant replies are short plain text. Summarising older turns is the upgrade path if context grows.

## 7. Error handling

| Situation | Result |
|---|---|
| `conversation_id` with no messages | 404; malformed id 422 |
| Groq or graph failure | 503 as today; nothing saved; the user can resend |
| Approval check fails or is unusable | Treated as OTHER |
| Approved in chat, campaign creation fails | 503; workflow still waiting; "yes" again retries without duplicating |
| Campaign created, saving the turn fails | The workflow is no longer waiting, so the next message is handled normally |
| Button and chat "yes" race | One wins; the other gets the existing 409 |

## 8. Testing

All with the scripted model, real tools, the test database and the real checkpointer.

- The section 1 demo as one conversation: the second agent call contains the first exchange; the planner input
  contains the history and the proposal's category is Jeans.
- History window: with 12 stored messages, the model receives exactly the last 10.
- New conversation when no id; 404 for an id with no messages; 422 malformed.
- Chat approval: "yes" gives `EXECUTED` and one campaign; "no" gives `REJECTED`; "make it 10%" is OTHER and stays
  pending; unusable verdict is OTHER; no approval call is made when nothing is pending.
- After a button approval, the conversation has the outcome message and the next "yes" is a normal message.
- A failed turn (provider error) saves no messages.
- Stored content of a `WAITING_APPROVAL` turn includes the proposal details.
- The migration runs through the existing `upgrade head` fixture.
- Every existing test still passes; chat without `conversation_id` behaves as before and returns a `conversation_id`.
