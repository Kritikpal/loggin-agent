# Guardrails for the AI operator — design

Status: draft for review · Date: 2026-10-27 · Approach: A (deterministic, in-repo, no new dependencies)

## Problem statement

`/ai/chat` passes the manager's message, database text (product names, customer records) and saved notes to the
model, and passes the model's reply and drafted campaign message back to people. Today the only protections are
prompt-level (`untrusted()` tags), the campaign rules in `validate_plan`, and human approval. Nothing deterministic
stops injected instructions reaching the model, an invented number reaching the manager, or a misleading
customer-facing message reaching the approval screen. The risk grows when the APIs open to more users.

## Goals

1. Text carrying prompt-injection markers (instruction overrides, fake role tags, hidden characters) never reaches
   a model, whether it comes from the user or from a tool result.
2. Every number in an assistant reply comes from this turn's tool results, the user's words, the conversation or
   today's date.
3. Replies never contain contact details or claims that the assistant itself created or changed something.
4. A drafted campaign message has no links, contact details, banned claims or a discount that differs from the
   proposal.
5. Every block is logged with enough context to review false positives.

## Non-goals

- **LLM safety classifier (approach B).** Only the trusted manager uses the chat today; add it together with
  authorization when the APIs open.
- **Guardrail framework (NeMo Guardrails, Guardrails AI, LLM Guard).** Heavy dependency for one action and one user.
- **Database table / UI for trips.** Log lines are enough until someone needs to review trips in the app.
- **Profanity list.** The manager reads every campaign message before approving it.
- **General action-policy engine.** No new action is planned. See the rule in *Future actions* instead.
- **Rate limiting.** Belongs with the authorization work.

## User stories

Store manager:
- As the manager, I want messages with injected instructions refused before any model call, so that nobody can
  steer the assistant through text I paste or forward.
- As the manager, I want data containing instructions (e.g. a product named "ignore previous instructions") withheld
  from the model, so that database content cannot hijack an answer.
- As the manager, I want every number I'm shown to come from real data, so that I can act on the answer.
- As the manager, I want a campaign draft rejected when its text promises something false or a different discount,
  so that I don't approve a misleading message by accident.
- As the manager, when something is blocked, I want a short neutral reply that says nothing changed, so that I
  know the state of the system.

Developer / operator:
- As the developer, I want one log line per block naming the point and the check, so that I can tune patterns that
  block normal text.

## Design

### Where the checks run

One new module, `app/ai/guardrails.py`: pure functions, no model calls.

```
chat_turn ──► [1 input] ──► agent_node ──► decision_node ──► planner ──► validate_node ──► approval
                              │                                              │
                     [2 tool results]                                [4 campaign message]
                              │
                       [3 agent reply]
```

| Point | Hook | On trip |
|---|---|---|
| 1 Input | top of `chat_turn`, before any model call | return `BLOCKED`; no model call, the turn is not stored, nothing offered to memory |
| 2 Tool results | `_tool_errors_to_model` middleware in `business_agent.py` | model receives `WITHHELD_TEXT` instead of the result; the turn continues |
| 3 Agent reply | end of `agent_node` | reply replaced by `BLOCKED_TEXT`, status `BLOCKED`, turn ends |
| 4 Campaign message | `validate_plan` in `actions/campaign.py` | `INVALID_ACTION` with the reason (existing path) |

- `WorkflowStatus` gains `"BLOCKED"`. `BLOCKED_TEXT = "I can't help with that request. Nothing was changed."`
- `WITHHELD_TEXT = "This data was withheld by a safety check."`
- The user is never told which check fired, so the patterns can't be probed. At point 4 the reason is shown,
  because it is about the draft's content, not about an attack (for example "message: discount 50% does not
  match the proposal's 15%").
- A blocked reply (point 3) is stored as `BLOCKED_TEXT`, so the conversation stays consistent. A blocked input
  (point 1) is not stored.

### Normalization

All matching runs on `normalize(text)`: Unicode NFKC, `casefold()`, whitespace collapsed to single spaces. The
hidden-character check runs on the raw text, before normalization.

### Checks

Each check is `def check(text: str, ...) -> str | None`, returning the check's name on a trip, else `None`.

**`injection` (points 1, 2, 4)**
- Instruction overrides: `(ignore|disregard|forget) (all|any|the)? (previous|prior|above|earlier)? (instructions|rules|prompt)`,
  `you are now`, `new instructions`, `system prompt`, `developer mode`, `jailbreak`.
- Fake role tags: `<system>`, `</?untrusted_\w+>`, `[inst]`, `### system`, or `system:` / `assistant:` at the
  start of a line.
- Hidden characters, on the raw text: U+200B–U+200F, U+2060, U+FEFF, U+202A–U+202E, U+2066–U+2069, and
  U+E0000–U+E007F.
- The patterns live in one tuple constant, `INJECTION_PATTERNS`. They are grown from the test phrasings, the same
  way as `routing.py`.

**`contact_details` (points 3, 4)**: an email (`\S+@\S+\.\w+`) or a phone number (7 or more digits, allowing
spaces, dots, dashes, parentheses and a leading `+`). Dates and money amounts must not match; the tests pin this.

**`action_claim` (point 3)**: `\bi(?:'ve| have)? (?:created|launched|sent|changed|updated|applied|scheduled|deleted)\b`.

**`ungrounded_number` (point 3)**: `grounded_numbers(reply, sources) -> list[str]` returns the numbers in the
reply that no source supports.
- Sources: this turn's tool results, the user message, the history contents, and today's date (year, month and
  day as numbers).
- Numbers are extracted with commas, currency signs and a trailing `%` stripped, then parsed as `Decimal`.
- A reply number matches a source number if it is equal, or equal after rounding the source to the reply's
  precision (1234.56 → 1235, 1234.6, or 1.2 when followed by `k`).
- Calculated numbers are not allowed (decision (a)). The tools supply them instead; see *Tool changes*.

**`campaign_text` (point 4)**, on `message`, with `name` included for the injection and link checks:
- Links: `https?://`, `www.`, or `\b[\w-]+\.(com|net|org|in|io|co|shop|store)\b`.
- Contact details and injection: as above.
- Banned claims, in the tuple constant `BANNED_CLAIMS`: `guarantee`, `risk-free`, `free`, `best price`,
  `lowest price`, `100%`.
- Every `N%` in the message must equal `discount_percent` (compared as `Decimal`).

### Tool changes (needed for the number check)

- New read-only tool `compare_sales_periods(a_start, a_end, b_start, b_end, category=None)` in
  `app/ai/tools/sales.py`. It calls `SaleService.summary` once per period and returns both periods' revenue,
  units and sale count, plus `revenue_change` and `revenue_change_percent` (null when period A's revenue is 0).
  It is registered in `build_agent`.
- `get_sales_summary` adds `share_percent` (category revenue ÷ total, 2 decimal places) to each category row, in
  the tool output only. The API schema is unchanged.
- `prompts/business_agent_system.md` gains one rule: "Only state numbers a tool returned. Use
  compare_sales_periods for changes between periods and share_percent for shares."

### Logging

One line per trip, through `logging.getLogger("app.ai.guardrails")`:

```
guardrail_trip point=<input|tool_result|reply|campaign> check=<name> conversation=<id|-> sample=<first 80 chars, repr>
```

For point 2 the tool name is added (`tool=<name>`). Cleanup: remove `logger.info(state)` from
`business_agent.py`, which logs the full agent state, tool data included, on every turn.

### Future actions

Every new action ships with three things: its own deterministic validation (like `validate_plan`), the
`campaign_text` checks (or equivalent) on any customer-facing text, and human approval. A shared policy layer
waits until a second action exists.

## Requirements

### P0 — must have

1. **Input block.** Given a message matching `injection`, when it is posted to `/ai/chat`, then the response
   status is `BLOCKED` with `BLOCKED_TEXT`, no model is called, no chat message is stored, and no memory task is
   scheduled.
2. **Tool-result withhold.** Given a product whose name contains an injection marker, when the agent calls a tool
   that returns it, then the model sees `WITHHELD_TEXT` for that call and the turn continues.
3. **Reply block.** Given a scripted reply containing a number that appears in no source, or an email or phone
   number, or "I've created", when the turn runs, then the response is `BLOCKED` with `BLOCKED_TEXT`, and that
   text is what gets stored.
4. **Campaign block.** Given a plan whose message says "50% off" with `discount_percent=15`, or that contains a
   link or a banned claim, then the status is `INVALID_ACTION` with a reason, and nothing waits for approval.
5. **No false blocks on normal text.** Product names, ISO dates, "top 5", rounded tool numbers and percentages
   from `compare_sales_periods` and `share_percent` all pass.
6. **Logging.** Every trip writes exactly one `guardrail_trip` line, at INFO or above.
7. **Calculated numbers come from tools.** `compare_sales_periods` exists and `share_percent` is in the summary
   output.
8. **Cleanup.** `logger.info(state)` is removed.

### P1 — nice to have

- `GUARDRAILS_ENABLED` setting in `config.py`, default true, for an emergency off-switch.

### P2 — future

- An LLM safety classifier as an extra input check, added with authorization.
- Storing trips in a table and listing them in the UI.
- Rate limiting per user once authorization exists.

## Testing

TDD: write the tests first, in the existing style.

- `tests/test_guardrails.py`: unit tests on the pure functions. Each check gets parametrized phrasings that must
  trip and normal business text that must pass.
- `tests/test_ai_guardrails_chat.py`: end-to-end through `/ai/chat` with `ScriptedModel`, one test per P0 item
  1–4, plus `caplog` for item 6.
- `tests/test_ai_sales_tool.py`: tests for `compare_sales_periods` (both periods, zero-revenue base) and
  `share_percent`.
- **Known risk:** existing scripted replies in `tests/test_ai_*.py` may contain numbers that aren't in their tool
  results, and those tests will start returning `BLOCKED`. Fix the scripted replies so they are grounded; do not
  loosen the check.

## Success metrics

- **Leading (first week of use):** P0 tests pass, and the trip log has fewer than 1 false block per 50 manager
  turns. Each trip is reviewed by hand from the log.
- **Lagging (first month):** no invented numbers or misleading campaign drafts are reported by the manager, and no
  pattern has to be removed for blocking normal text more than once.

## Open questions

- **Non-blocking (engineering):** if the number check proves noisy in real use, add more tool-supplied numbers
  rather than relaxing the check. Revisit after a week of trip logs.
- **Non-blocking (stakeholder):** confirm the banned-claims list with whoever owns customer messaging. "free"
  also blocks "free shipping", which may be a legitimate offer.

## Timeline and phasing

There are no hard deadlines. The work fits in one implementation plan, in this order:

1. The guardrails module and its unit tests.
2. The tool changes.
3. The four hook points and the end-to-end tests.
4. Fixing existing tests.
5. The cleanup.

Approach B depends on the authorization work and is not part of this plan.
