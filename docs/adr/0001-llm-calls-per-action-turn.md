# ADR-001: Keep the separate router and planner calls for action requests (4-5 LLM calls per turn)

**Status**: Proposed
**Date**: 2026-10-26
**Deciders**: Project owner (to confirm)

## Context

A chat turn that asks for a change (for example "create a 15% campaign for jeans") makes these model calls in
`app/ai/graphs/operator_graph.py`:

| # | Call | Purpose |
|---|------|---------|
| 1-3 | `agent_node` | Read-only tool-calling agent: 1 call for the first model turn, +1 per tool round, +1 for the final write-up. Typically 2-3. |
| 1 | `decision_node` | Router: ANSWER or ACTION (structured output). |
| 1 | `planner_node` | Turns the request and the agent's findings into an `ActionPlan` (structured output). |
| 1 | `remember` (background) | Extracts long-term memory after the reply is sent. Not on the request path. |

That is 4-5 calls on the request path (agent 2-3, router 1, planner 1), plus 1 in the background. A plain question
skips the planner: 3-4 on the request path, plus 1 in the background. Approving a pending proposal is 1 call (`classify_approval`) and executes with no
further model call. Memory recall is a database query, not a model call.

Constraints and forces:
- An action changes real data (a campaign row), so a wrong "this is just a question" must never slip through.
  `decision_node` already fails closed: an unusable verdict becomes ACTION.
- Nothing runs without human approval; `validate_node` checks the plan before it is held for approval.
- The planner needs the agent's findings ("restock what's low" style requests investigate first), so the agent
  must run before the planner.
- No latency or cost measurements exist yet; this ADR compares options by call count and design, not by data.
- Related: the semantic-memory spec (`docs/superpowers/specs/2026-10-26-semantic-memory-design.md`) added the
  background `remember` call and the notes that grow every prompt.

## Decision

We will keep the separate router and planner calls for now, and not merge them into the agent.

The deciding trade-off is safety and debuggability over call count. Three narrow, single-purpose structured calls
are each easy to test and fail closed on their own. Folding routing and planning into the free-form agent would
save 2 calls but make "did the model decide to change something?" depend on one long, tool-using generation.

This is worth revisiting as soon as we have per-call latency and token numbers (see Review Triggers). The router is
the weakest-value call: it runs on every turn, and its question is answerable from the same context the agent has
already seen.

## Consequences

### What becomes easier
- Each stage has its own prompt in `prompts/` and its own test; a routing bug does not require touching the agent.
- Fail-closed behaviour is explicit and local (`decision_node`, `validate_node`).
- The human-approval boundary stays a hard graph edge, not a model's choice.

### What becomes harder
- Action turns cost about 2 more calls than a single-agent design, so they are slower and pricier.
- Prompt tokens add up: notes and history are sent to the agent, router, planner and extractor.
- Every call is another place a provider error can occur (`_structured` returns None and the graph degrades).

### Technical debt accepted
- The router runs even when the agent's own reply makes the route obvious.
- All calls use the same model; routing and approval classification do not need the strongest one.

## Alternatives Considered

### Option A: Keep router + planner as separate calls (chosen)
- **Pros**: Clear separation, fail-closed router, small focused prompts, already built and tested (282 passing).
- **Cons**: 3-4 request-path calls for a question and 4-5 for an action; router cost on every turn.
- **Risks**: Latency and cost may matter at scale; unmeasured today.
- **Effort**: None (S to keep).
- **Why chosen**: Safest option, no rework, and the cost is unproven.

### Option B: Merge the router into the planner
The planner returns NONE, CREATE_CAMPAIGN or UNSUPPORTED; NONE means ANSWER. This removes `decision_node`.
- **Pros**: Saves 1 call on action turns; answer turns stay the same count (planner replaces router).
- **Cons**: Planner prompt and schema get bigger and run on every turn; ambiguity between NONE and UNSUPPORTED;
  fail-closed behaviour must be redesigned.
- **Risks**: A subtle regression that lets an action request through as an answer; needs eval cases first.
- **Effort**: M.
- **Why not (yet)**: Saves only 1 call and touches the safety path; do it only with measurements and tests.

### Option C: One agent with a `propose_campaign` tool
The agent calls a tool to create the proposal; there is no router and no planner.
- **Pros**: Fewest calls (about 2-3 on the request path, no router or planner); simplest graph.
- **Cons**: Routing and planning become part of one free-form generation; harder to test; weaker independent check
  before a change is proposed; `validate_node` and the approval edge would still be needed.
- **Risks**: Prompt injection through tool data could steer the proposal; the model may propose when the user only
  asked a question.
- **Effort**: L.
- **Why not**: Trades the explicit safety structure for speed, which is hard to justify without evidence.

### Option D: Cheaper router (rules first, or a smaller model)
Keep the structure but route obvious cases without an LLM (e.g. no change-verbs means ANSWER) or send router,
approval and extract calls to a small model.
- **Pros**: Same safety structure; cuts cost and latency of the least valuable calls; request-path calls can drop
  to 2-3 on questions if rules are confident.
- **Cons**: Rules drift and need maintenance; a second model adds configuration. Rules must fail toward ACTION.
- **Risks**: Keyword rules miss phrasing ("let's do 10% off jeans") and misroute.
- **Effort**: S for a smaller model, M for rules.
- **Why not (yet)**: Good first optimisation once numbers show a problem; not needed to accept this ADR.

### Option E: Do nothing differently, ignore the question
Same as Option A with no follow-up.
- **Pros**: No effort.
- **Cons**: Cost and latency stay unmeasured, so we cannot tell if 4-5 calls is acceptable.
- **Why not**: We should at least measure; see Follow-up.

## Follow-up

- Log per-call latency and token counts (agent, router, planner, approval, extract) per chat turn.
- Build a small eval set of ~30 phrasings (questions, actions, mixed, ambiguous) to test any routing change.

## Implementation status (2026-10-26)

Option A is kept; the first two steps of Option D are in:

- **Fast model**: `GROQ_FAST_MODEL` (unset = use `GROQ_MODEL`) serves the router and the approval check. The agent,
  planner and memory extract stay on `GROQ_MODEL`. Suggested value: `openai/gpt-oss-20b`.
- **Call log**: every model call logs `llm_call step=… model=… ms=… input_tokens=… output_tokens=…`
  (`app/ai/telemetry.py`; steps: agent, Decision, ActionPlan, ApprovalVerdict, Extraction).
- **Router shortcut** (`app/ai/routing.py`): if neither the message nor the assistant's last reply contains a
  change word, the router is skipped and the route is ANSWER. Anything else still goes to the LLM router, which fails
  closed to ACTION. Questions now take 2-3 request-path calls; action turns are unchanged at 4-5.
- **Phrasing set**: `tests/test_routing_rule.py` holds the question / change / mixed phrasings. Its invariant: a
  message that asks for a change, or might, is never shortcut.

Not done: shortcutting clear action requests, merging router into planner (Options B and C). Revisit them only
with the log's numbers in hand.

## Review Triggers

- [ ] Latency: Revisit if p95 action-turn latency exceeds ~15 s (target to be confirmed with the owner).
- [ ] Cost: Revisit if model spend per chat turn becomes a noticeable share of costs once real traffic exists.
- [ ] Scope: Revisit when a second action type (not just campaigns) is added; routing and planning get harder and
      Option B or C may look different.
- [ ] Technology: Revisit if the provider offers a cheap fast model suitable for Option D.
- [ ] Time-based: Review by 2027-04-26 regardless of other triggers.
