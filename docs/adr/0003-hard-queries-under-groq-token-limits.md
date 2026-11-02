# ADR-003: Handle multi-step questions within Groq's per-minute token limit

**Status**: Accepted for layers 2-3 (implemented 2026-11-02); layer 1 kept but no effect on gpt-oss-120b; layer 4 open
**Date**: 2026-11-01
**Deciders**: Project owner (to confirm)

## Context

Hard questions need several tool calls, and each tool call costs another model call. On Groq's free tier the model
(`openai/gpt-oss-120b`) allows **8,000 tokens per minute per organisation**, and every model call re-sends the whole
conversation so far. Hard questions therefore fail with HTTP 429 (over the minute's budget) or 413 (one request over it).

Measured on the indexed fixture logs (tokens estimated as characters / 4):

| Part of every model call | Tokens |
|---|---|
| System prompt (rules + 4 sources with spans) | ~1,400 |
| Tool definitions sent to Groq (search_logs, summarize_logs) | ~1,000 (3,980 chars) |
| **Fixed cost per model call** | **~2,400** |

| Tool result | Tokens |
|---|---|
| summarize_logs | 75-150 |
| search_logs, 5 short hits | 450-500 |
| search_logs, 10 ERROR hits, short traces | ~700 |
| search_logs, 10 ERROR hits, long stack traces | ~2,700 |

Eval case b4 (a 4-part question) made 5 tool calls, so 6 model calls. Call *n* carries the fixed cost plus every earlier
result: about **23,000 input tokens in roughly a minute, about 3x the limit**, before the model's own output. It
passed only after 2 rate-limit failures and about 5 minutes. In the app the same question returns "AI unavailable".

Where the tokens go: **6 x 2,400 = 14,400 of the 23,000 is fixed cost.** The number of model calls matters more than
result size, and result size matters more than prompt wording.

Constraints: free tier (no paid upgrade assumed); 4 Groq keys configured, and the 429s so far came from 2 different
organisations (limits are per organisation, not per key); `ChatGroq` today has `max_retries=1` and picks one key per
question (`app/core/llm.py`); answers must keep citing source, line and time.

Related: ADR-002 (summarize_logs, whose small results already help).

## Decision

Four layers, each cheap and independent, in the order of tokens saved:

1. **Fewer model calls: parallel tool calls.** The prompt tells the model to request independent lookups in the
   same turn (b4's four parts need no answer from each other). The middleware budgets count tool calls, not turns, so
   the limits are unchanged. b4: 6 model calls -> about 3.
2. **Smaller fixed cost: shorter tool descriptions.** The search description is a strategy guide that duplicates the
   system prompt. Keep only argument semantics there. Measured: 3,980 -> 2,498 characters, about 1,000 -> 625 tokens, saving ~370
   tokens on every call (the rest is the JSON schema itself). An earlier estimate of 900 counted the descriptions twice.
3. **Smaller carried evidence: digest tool results the model has already read.** `DigestSeenResults`
   (`app/agent/context.py`), a `ContextEdit` for LangChain's `ContextEditingMiddleware`, rewrites `search_logs` results
   from earlier turns, in the copy sent to the model only, as one line per hit: `source L158-163 LEVEL time | first
   line [...] last "Caused by:" line`. A log line is kept whole up to 300 characters; a stack trace loses its frames.
   The latest turn's results, not read yet, stay full; summaries stay as they are; the API response and the coverage
   note keep the full results. Measured on the fixtures: 10 WSO2 errors 1,661 -> 912 tokens, 10 dev-auth errors
   702 -> 295, 5 short INFO hits 458 -> 320.
   A first version cut every event to its first 110 and last 50 characters. The eval (t1) showed the cost: it removed
   "PID 26512" from the middle of a 239-character line, the agent searched for the PID twice more, hit the search
   limit, and answered that no PID was logged. Facts live in a line's message, so lines are now kept whole.
4. **Wait instead of fail: a per-key token pacer plus key failover.**
   - A small token-bucket limiter in front of the model: estimate the request's tokens (characters / 4 plus the
     output allowance), and if the key's last 60 seconds would go over 8,000, sleep until it fits. Groq's 429 says
     how long to wait ("try again in 82 ms"), so `ModelRetryMiddleware` (installed) retries 429s with backoff as a backstop.
   - On a 429 that would wait more than a few seconds, move the call to a key in another organisation
     (`ModelFallbackMiddleware` with one `ChatGroq` per organisation). Keys in the same organisation are grouped, since
     extra keys there add nothing.
   - A 413 (one request over the limit) is never retried; layers 1-3 keep requests far below it.

Expected for b4: about 3 model calls of 2,000-4,000 input tokens each, **~9,000-10,000 tokens instead of ~23,000**.
The pacer spreads the small overflow over the next minute, so the question finishes in about 1-1.5 minutes with no
error, or faster when a second organisation's key is free.

Why this order: layers 1-3 remove tokens and are worth having even on a paid tier. Layer 4 only turns the remaining
overflow into waiting. Paying for Groq's Dev tier would remove the problem without code, so this design assumes that is
not an option yet.

What would make this wrong: if the model ignores the parallel-call instruction (gpt-oss usually supports it; the eval
shows it), layer 1 saves nothing and the pacer makes hard questions slow (2-3 minutes) rather than failing.

## Results (2026-11-02)

Measured with the token golden set (`tests/fixtures/agent-tokens-golden.yaml`, 8 developer questions) and LangSmith
traces (projects `log-agent-eval`, `log-agent-eval-gptoss`, `log-agent-eval-qwen`; results in `docs/compare-*.json`).

**Layer 1 has no effect on `gpt-oss-120b`.** Asked directly for two independent tool calls, with
`parallel_tool_calls=True` and an explicit instruction, it returned exactly 1 call per reply (3 of 3 tries);
`gpt-oss-20b` returned none. `qwen/qwen3.8-27b`, the only other tool-capable chat model the 4 keys can use,
returned 2 every time. The prompt rule stays (harmless, ~40 tokens) for a future model that batches.

**qwen3.8-27b was rejected as a replacement.** It batched (t3: 2 model calls instead of 3) but called tools that do
not exist 5 times (`summarize_files`, `search_all_logs`, `search_query`...; Groq refuses the request and the question
fails), went over its lower limit (7,000 input tokens a minute) 4 times, and exceeded the call budgets twice.
Smoke set: 8/12 against 12/12 for gpt-oss-120b. It used no fewer tokens per question.

**Layers 2 and 3 on gpt-oss-120b** (tokens per question from the traces):

| Question | Model calls | Total tokens | Largest request |
|---|---|---|---|
| t1 boot failure + PID | 5 | 15,235 (22,917 before the digest fix) | 4,039 |
| t2 error count + user | 4 | 15,315 | 5,956 |
| t3 health check, 2 sources | 3 | 7,743 | 2,941 |
| t4 checkout + other errors | 4 | 11,940 | 3,373 |
| t5 jwtAuthFilter cause chain | 5 | 17,198 | 4,536 |
| t6 all WSO2 errors | 2 | 7,607 | 4,312 |
| t7 IST incident window | 3 | 11,141 | 4,238 |
| t8 Java version | 2 | 4,902 | 2,750 |

No request came near the 8,000 limit (largest 5,956), so no question can fail with 413 any more. Questions still
total 5,000-17,000 tokens, so two hard questions in one minute will still hit 429: that is layer 4's job.
Correct answers 6/8. The two misses (t2, t6) both lost the user id `35eea954...`: in the index the "claims for user"
part is redacted as an e-mail address, so the id survives only in the "Caused by: ... UserNotFound: User 35eea954...
does not exist" line, and the 4,000-character guard (400 characters per hit for 10 hits, head and tail) cuts it.
Fixed the same day: hits and digests now share one line-based rule (`shorten_event`: the message up to the first stack frame, at least 450 characters for a fresh hit, plus the last "Caused by"). t6 then found the id; a 10-error WSO2 result costs ~1,920 tokens instead of ~1,661. t2 failed again for another reason: gpt-oss-120b wrote a raw tool call (`<|channel|>commentary to=functions...`) into its answer, which ends there (also seen once in the smoke set, q5).

## Consequences

### What becomes easier
- Multi-part and "why" questions with 4-6 lookups finish instead of returning "AI unavailable".
- Every question gets cheaper: the description trim and digests save tokens on simple questions too.
- Spare keys in other organisations add real capacity instead of sitting idle.

### What becomes harder
- Hard questions can take 1-2 minutes. The chat endpoint must allow that (today each model call has a 30 s timeout,
  and the HTTP request has none of its own; a proxy in front would need one longer than 2 minutes).
- The pacer keeps state per process. Two app processes share a key without knowing it (fine for one local process).
- The model sees digests, not full text, of older results. A later step that needs a trace's middle again must search
  or reopen it.

### Technical debt accepted
- Token counts are estimated (characters / 4), not tokenised; the pacer leaves a 10% margin.
- Key-to-organisation grouping is configured by hand (`GROQ_KEY_ORGS` or a naming convention), because Groq does not
  expose it before the first 429.

### Follow-up work
1. Prompt: "request independent lookups in the same turn". Graph test: a scripted model with 3 tool calls in one turn
   uses 3 of the budget and 2 model calls.
2. Trim `DESCRIPTION` in `app/agent/tools.py`; keep the existing description tests' facts.
3. `app/agent/context.py`: the digest edit plus a test (old results become digests, the latest stays full, citations
   fields kept).
4. `app/core/llm.py`: `TokenPacer` (per key, 60 s window) with a test using a fake clock; `max_retries` handled by
   `ModelRetryMiddleware` on 429 only; one model per organisation for fallback.
5. Re-run eval b4, ss4 and the smoke set; record tokens per question (add an approximate count to the eval report).

## Alternatives Considered

### Option A: Pay for Groq's Dev tier
- **Pros**: No code; much higher limits.
- **Cons**: Cost; the waste (fixed cost x calls) remains.
- **Why not**: Not assumed available. Layers 1-3 still pay off after an upgrade.

### Option B: Split hard questions into sub-questions (planner + one small agent run each + a combining call)
- **Pros**: Each run starts with an empty history; parts can run in parallel on different keys.
- **Cons**: 2 extra model calls (plan, combine), each carrying the fixed cost; a classifier to decide when to split;
  parts lose each other's findings ("why" chains need them).
- **Why not now**: Parallel tool calls give most of the gain inside one run. Revisit if questions with more than ~6
  lookups become common.

### Option C: Summarise the conversation (`SummarizationMiddleware`)
- **Pros**: Built in.
- **Cons**: Costs a model call to save tokens; a summary can drop line numbers needed for citations.
- **Why not**: A digest made in code is free and keeps every citation field.

### Option D: Only retry with backoff
- **Pros**: A one-line change (`ModelRetryMiddleware`).
- **Cons**: Does not reduce 23,000 tokens; b4 still needs 3 minutes, and a 413 can never succeed.
- **Why not alone**: Kept, but as the backstop of layer 4.

### Option E: Do nothing
- **Cons**: Hard questions keep failing in the app, exactly the questions where the agent is most useful.

## Review Triggers

- [ ] Provider: revisit layer 4 if the Groq tier or model changes (other limits, other token counts).
- [ ] Usage: if the eval shows the model rarely makes parallel calls, consider Option B.
- [ ] Scale: if more than one app process serves chat, move the pacer's state to shared storage (e.g. Redis).
- [ ] Size: revisit the digest length if citations start missing lines in the eval.
- [ ] Time-based: review by 2027-05-01.
