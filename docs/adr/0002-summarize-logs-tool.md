# ADR-002: Add a read-only `summarize_logs` tool that gives the agent an overview before it searches

**Status**: Accepted (implemented 2026-10-31)
**Date**: 2026-10-31
**Deciders**: Project owner (to confirm)

## Context

The investigation agent (`app/agent/graph.py`) has one tool, `search_logs` (`app/agent/tools.py`), with a hard budget
of 3 calls per question. Search is hybrid (pgvector + full-text, fused by RRF) over the `log_events` table and always
returns the nearest events, never counts.

Problems this leaves:
- **No overview.** The agent's first search is a guess. It cannot see where the problems are (which source, which
  level, which time range) before it spends a search.
- **Counting questions cannot be answered.** "How many errors?", "Is anything wrong overall?" and "When did the
  warnings start?" need every matching row, but search shows at most 5 hits (20 with a level filter).
- **"First" / "latest" questions are expensive.** The prompt tells the agent to list up to 20 events with a level
  filter and compare times itself, which costs a search and about 2,000+ tokens.
- **Token limit.** Groq's free tier allows 8,000 tokens a minute, and every tool result is re-sent with each later model
  call. Raw log text is the expensive part, so an answer built from counts is far cheaper than one built from hits.

What the database already supports, with no migration:
- `log_events.source_id`, `level`, `occurred_at`, with indexes `ix_log_events_source_time` (source_id, occurred_at)
  and `ix_log_events_level_time` (level, occurred_at).
- `source_index_state` for coverage, already reported by `coverage_for_all`.
- The search filters (`app/tools/search_queries.filters`) for sources, levels and time range.

Not available yet: `logger`, `exception_type` and `trace_id` are parsed during ingestion but only written into
`semantic_text`, not stored as columns (`app/ingestion/fields/resolver.py`). Grouping by them needs a migration.

The system prompt already lists each source's first and last event time (`build_system_prompt` spans), so the time
span itself is not new information. Counts and per-level times are.

Related: ADR-001 (number of LLM calls per turn); the planned `get_event_context` tool (read one event in full plus
its neighbours), which is a separate decision.

## Decision

We will add one read-only tool, `summarize_logs`, with its own budget of 2 calls per question:

```
summarize_logs(source_ids?: list[str], levels?: list[str], start_time?: datetime, end_time?: datetime)
```

It runs one aggregate query, grouped by source and level:

```sql
SELECT source_id, level, count(*), min(occurred_at), max(occurred_at)
FROM log_events WHERE <the same filters as search_logs> GROUP BY source_id, level
```

and returns those rows plus `coverage`, with no log text. A typical result is 200-400 tokens.

The system prompt gets one new step before "Broad": for an open question ("what's wrong?", "how many...", "first /
latest ..."), call `summarize_logs` first, then search where the counts point.

Why this option: it gives the agent the overview and the counts that search cannot, for the fewest tokens, without
using up any of the 3 searches. It needs no migration and no new index, and it reuses the search filters and source
allowlist, so the new attack surface is one parameterised SELECT.

What would make this wrong: if the agent calls it on every question, even precise ones like "SPRING_DATASOURCE_URL",
it adds one model round trip (about 1-2 s) for nothing. The prompt must keep it for open and counting questions only;
the eval set (`docs/agent-test-questions.md`) checks this.

## Consequences

### What becomes easier
- "How many errors / warnings?" is answered exactly, in one cheap call, not by listing hits.
- "When did the errors start / stop?" comes from `min` / `max`, with no search.
- Vague questions ("is anything wrong in dev-auth-service?") start from facts instead of a guessed query, so the 3
  searches go to the right source, level and time.
- Multi-part questions (eval #21) get their counting parts answered outside the search budget.

### What becomes harder
- The agent has two tools to choose from; the prompt has to say clearly when to use each.
- One more tool description is sent with every request (about 60-80 tokens).
- One more budget to enforce: a second `ToolCallLimitMiddleware` for `summarize_logs` (limit 2).

### New constraints
- The tool must stay read-only and must return no `raw_text`, so its output never carries log content (and so no
  injected instructions) into the model's context.
- It must use the same source allowlist (`get_source`) and the same filter code as `search_logs`, so the two tools
  never disagree about what is in scope.

### Technical debt accepted
- No grouping by exception type, logger or trace id until those fields are stored as columns. Grouping by level and
  source covers the counting questions in the current eval set.
- Events with no timestamp are counted but have no first/last time; they are reported as null rather than estimated.

### Follow-up work
1. `app/agent/tools.py`: `make_summarize_logs_tool`, reusing `filters()` and the source allowlist; errors returned as
   text like `search_logs`.
2. `app/agent/graph.py`: register the tool with a separate limit of 2.
3. `app/agent/prompt.py`: add the "Overview" step and when to skip it.
4. Tests: grouping and counts against the test database; unknown source refused; time filter applied; graph test for
   the separate budget.
5. Re-run the eval set; add 2 questions: "How many errors are in dev-auth-service?" and "When did the first warning
   happen?".

## Alternatives Considered

### Option A: Put an overview in the system prompt
Compute counts per source and level at startup or per request and add them to the prompt, as the time spans are now.
- **Pros**: No tool call, no extra round trip; the agent always has the overview.
- **Cons**: Paid on every request, including precise ones that don't need it; goes stale between indexing runs unless
  recomputed per request; cannot be narrowed to a time range or a source the user names.
- **Why not**: It costs tokens on every question and cannot answer "how many errors between 10:00 and 10:15?".

### Option B: Add a `mode="count"` option to `search_logs`
Same aggregate, reached through the existing tool.
- **Pros**: No new tool name.
- **Cons**: The search description, already the longest text the agent reads, grows further; a count call would use one
  of the 3 searches; the response shape changes by mode, which is harder for the model and for tests.
- **Why not**: It takes from the search budget, which is the scarce resource this decision is meant to protect.

### Option C: Full analytics tool (group by any field, histograms, top messages)
- **Pros**: Answers more question types.
- **Cons**: Needs the migration for exception type, logger and trace id; more arguments and a longer description;
  larger results; more ways for the model to build a bad query.
- **Why not**: No current eval question needs it. Revisit after the migration (see triggers).

### Option D: Do nothing
- **Pros**: No effort, no new surface.
- **Cons**: Counting questions stay unanswerable; "first / latest" keeps costing a search and about 2,000 tokens; vague
  questions keep starting with a guessed search.
- **Why not**: Counting and overview questions are common for a log assistant, and search cannot answer them.

## Review Triggers

- [ ] Data change: revisit grouping when `exception_type` / `logger` / `trace_id` are stored as columns; add them as
      `group_by` options then.
- [ ] Scale: revisit if the aggregate query takes over 200 ms (for example past a few million rows per source); add a
      materialised daily summary or a narrower index then.
- [ ] Usage: revisit if the eval or logs show the agent calling `summarize_logs` on precise identifier questions, or
      calling it twice per question as a routine.
- [ ] Provider: revisit the 2-call limit and result size if the model moves off Groq's 8,000 tokens-a-minute tier.
- [ ] Time-based: review by 2027-04-30.
