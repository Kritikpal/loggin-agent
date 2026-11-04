# Push log ingestion

Date: 2026-11-04 · Status: draft for review · Branch: feat/ai-action-approval-workflow

## Goal

Let an application push its logs to this service over HTTP, continuously. Duplicate events (sender retries, overlapping
batches) are dropped, and everything else is stored in `log_events` so search and the agent see pushed logs exactly
like logs read from files.

## Agreed with the user

- **Delivery:** the sender pushes to us (webhook). We do not poll anything.
- **Duplicate = same content.** Key is a hash of the redacted event text (plus the sender's timestamp when present).
  Two truly identical lines with the same timestamp collapse to one; that is accepted.
- **Reuse** redaction, parsing, signatures and the Postgres store. New code is a reader-side endpoint, a store
  function and a background embedding loop.

## Non-goals

Rate limiting, an external queue (Kafka/Redis), counting repeats (`count`, first/last seen), pulling from remote
APIs or log platforms, retention/cleanup. Each can be added later without changing this design.

## Design

### 1. Declaring a push source

A project opts in with one new `project.yaml` key:

```yaml
push_token_env: ACME_PUSH_TOKEN   # name of the environment variable holding this project's bearer token
```

- `KEYS` in `app/ingestion/projects.py` gains `push_token_env`; it must match `[A-Z][A-Z0-9_]{0,63}`, else `ConfigError`.
- A project with the key has one extra source, id `<project>/push`. `Project` gets `push_source: PushSource | None` and a
  `sources` property (`files` + the push source). The token value is never stored in `Project`; it is read from the
  environment per request.
- The id `push` is reserved: a file whose stem is `push` is skipped with a reason, like any other id clash.
- If the env var is unset or empty, push is disabled for that project and the endpoint answers the same 404 as an
  unknown project (nothing is revealed about why).

Search, the agent tools and coverage today use `project.files`. They must use `project.sources` so pushed events are
searchable and show up in coverage (`app/agent/prompt.py`, `app/agent/tools/{find,overview,search_logs}.py`,
`app/controllers/dependencies.py`, `app/controllers/projects.py`, `app/ingestion/coverage.py`). `orphan_ids` must
treat the push source id as known, otherwise it is listed as "missing". In the files listing the push source appears
as a row named `push`.

### 2. Endpoint

`POST /api/v1/projects/{project_id}/ingest` in a new `app/controllers/ingest.py`, registered in `router.py`.

- **Auth:** `Authorization: Bearer <token>`, compared with `hmac.compare_digest`. Missing or wrong: 401 with a generic
  message.
- **Body:** `application/json` list of events, or `application/x-ndjson`. Event: `{"message": str, "timestamp"?: str,
  "level"?: str}`. Extra keys are ignored.
- **Limits:** body at most 1 MiB, at most 1000 events, a message longer than 256 KiB (the cap a file event gets) is clipped after
  redaction, not rejected. A `message` that is empty after control characters are removed is a 422. Over the body or count limit: 413.
- **Reply 200:** `{"received": n, "stored": n, "duplicates": n}`. Pydantic response model.
- **Errors:** malformed body 422; database unreachable 503 (sender should retry; any other database error is a 500).
  An invalid project is a 409 only after the token checks out. The token is looked up in the environment, then in the
  `.env` values `Settings` keeps. A sender `level` outside TRACE/DEBUG/INFO/WARN/WARNING/ERROR/SEVERE/FATAL/CRITICAL
  is ignored. Error text never echoes log content.

### 3. Processing (request path)

Per event, in the request, without embedding:

1. `redact` the message (same function the file path uses; redaction first, everything derives from redacted text).
2. If the sender gave an ISO 8601 `timestamp` that parses, it becomes `occurred_at` (a naive one is read in the
   project's timezone); otherwise `resolve()` finds the time from the message text as it does for files. A sent
   `level` is used only when the parser finds none.
3. Reuse `_prepare` from `app/ingestion/pipeline.py` (made public as `prepare_event`) to get level, fields, semantic
   text, signature. No second parsing implementation.
4. `event_key = "push:" + sha256(redacted_text + "\n" + sender_timestamp_or_empty)`. `locator = {"kind": "push"}`.

### 4. Store

New `insert_new_events(session, source_id, rows) -> int` in `app/ingestion/store.py`: the same insert as
`upsert_events` but `ON CONFLICT (source_id, event_key) DO NOTHING`, returning the number of rows actually inserted
(`stored`); `duplicates = received - stored`. A retry therefore changes nothing and never triggers a re-embed. Rows are
inserted with `embedding = NULL`. The whole request is one transaction. The file path keeps `upsert_events` as is.

Duplicates inside one request are dropped too: `DO NOTHING` handles them, and they are counted as duplicates.

### 5. Continuous part

`lifespan` in `app/main.py` starts one `asyncio` task that loops: `reembed_missing(session_factory)`, then sleeps
`PUSH_EMBED_INTERVAL_SECONDS` (setting, default 5). On cancel at shutdown it stops cleanly before `engine.dispose()`.
`reembed_missing` already stops at the first failing chunk, and the loop catches any exception, logs the class name
only, and carries on. One loop per process; if the app runs several workers, extra loops are harmless (rows are
filled by id, updates are idempotent).

Pushed rows are searchable by keyword immediately (`search_tsv` is generated) and by meaning once embedded.

## Security

- Token per project, constant-time compare, never logged, never in responses.
- Logs are untrusted input: redaction before anything is stored or hashed; body size, event count and message length
  limited; errors carry exception class names only.
- Endpoint writes only to `log_events` for a source that exists in the scanned project list; `project_id` is looked up,
  never joined into a path.
- Plain HTTP carries the token: deploy behind TLS. Documented, not enforced here.

## Testing

pytest, matching the existing style (`tests/test_pipeline.py`, `tests/test_store.py`):

- Same batch pushed twice: second reply is `stored: 0`, row count unchanged.
- Duplicate inside one batch counts as a duplicate.
- Missing, wrong, and right token (401, 401, 200). Unset env var and unknown project both 404.
- 413 for too many events and for an oversize body; 422 for malformed JSON.
- A secret in a message is redacted in `raw_text`, and two messages differing only in the secret are duplicates
  (the key is built from redacted text).
- Pushed event appears in `project.sources` coverage and is found by the search tool for its project only.
- Embedding loop: rows pushed with `embedding NULL` get a vector after one `reembed_missing` pass (fake embedder).
- Loop survives an embedder that raises.

Coverage target 80% for the new code.

## Open risks

- Same-content dedup can hide two genuine identical events with identical timestamps. Accepted by the user; the
  upgrade path is the "count repeats" option.
- Changing `project.files` to `project.sources` touches several agent modules; the existing golden eval should be
  re-run after the change to confirm no regression in the agent's behaviour.
