# Push Log Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An application can push logs over HTTP to a project; duplicates are dropped by content hash, new events land in `log_events`, and a background loop embeds them.

**Architecture:** A project opts in with `push_token_env` in `project.yaml`, which adds a `PushSource` (`<project>/push`) next to its file sources. `POST /api/v1/projects/{project_id}/ingest` redacts and parses each event with the existing code, inserts rows with `ON CONFLICT DO NOTHING` and no vector, and replies at once. A loop started in the app lifespan runs `reembed_missing` to fill vectors.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2 async + PostgreSQL/pgvector, pytest (asyncio auto mode, `integration` mark for DB tests).

**Spec:** `docs/superpowers/specs/2026-11-04-push-log-ingestion-design.md`

## Global Constraints

- Push source id is `<project>/push`; the stem `push` is reserved (a file whose stem is `push` is skipped with a reason).
- `push_token_env` must match `[A-Z][A-Z0-9_]{0,63}`, else `ConfigError`. The token value is read from the environment per request and never stored on `Project`, logged, or returned.
- Auth: `Authorization: Bearer <token>`, compared with `hmac.compare_digest`. Missing/wrong: 401 with a generic message.
- Unknown project, project without push, and unset/empty token env var all give the same 404 (`NOT_FOUND` message from `app.ingestion.projects`).
- Limits: body at most 1 MiB and at most 1000 events, else 413. Malformed body: 422. Database unavailable: 503.
- Reply 200: `{"received": n, "stored": n, "duplicates": n}`.
- `event_key = "push:" + sha256(redacted_text + "\n" + sender_timestamp_or_empty).hexdigest()`; `locator = {"kind": "push"}`.
- Redaction runs first; everything stored or hashed derives from redacted text. Errors log exception class names only, never log content.
- Insert uses `ON CONFLICT (source_id, event_key) DO NOTHING`; rows have `embedding = NULL`; one transaction per request. The file path keeps `upsert_events` unchanged.
- Setting `PUSH_EMBED_INTERVAL_SECONDS`, default 5, controls the embedding loop. One loop per process.
- Coverage target 80% for the new code.

## Review Focus

- Message that is empty or whitespace only: 422, nothing stored (Task 4).
- Garbage or naive `timestamp`: never a 500; garbage falls back to the parsed time, naive is read in the project timezone (Task 4).
- Body that is invalid JSON, not UTF-8, or neither list nor NDJSON: 422, not 500 (Task 4).
- The same event pushed by two requests at once: exactly one row (Task 3).
- Push to a project whose `project.yaml` is invalid: 409 like the other project routes (Task 4).

## File Structure

- Modify `app/ingestion/sources.py`: add `PushSource`; `Source` becomes `FileSource | PushSource`.
- Modify `app/ingestion/projects.py`: `push_token_env` key, `Project.push_source`, `Project.sources`, reserved stem.
- Modify `app/ingestion/coverage.py`, `app/controllers/projects.py`, `app/controllers/dependencies.py`, `app/agent/prompt.py`, `app/agent/tools/{find,overview,search_logs}.py`: use `project.sources`; handle a source without a file.
- Modify `app/ingestion/store.py`: `insert_new_events`.
- Modify `app/ingestion/pipeline.py`: rename `_prepare` to public `prepare_event`, accept `SourceBase`.
- Create `app/ingestion/push.py`: event key, timestamp handling, `build_push_rows`, token check.
- Create `app/schemas/ingest.py`: `PushEvent`, `IngestResponse`.
- Create `app/controllers/ingest.py`; modify `app/controllers/router.py`; modify `app/core/errors.py` (new errors).
- Create `app/ingestion/embed_loop.py`; modify `app/main.py` and `app/core/config.py`.
- Modify `tests/project_helpers.py`: `make_project(..., push_source=None)`.

---

### Task 1: Push source declaration

**Files:**
- Modify: `app/ingestion/sources.py`, `app/ingestion/projects.py`, `tests/project_helpers.py`
- Test: `tests/test_push_source.py`

**Interfaces:**
- Produces: `PushSource(SourceBase)` frozen dataclass with `token_env: str`; `Source = FileSource | PushSource`; `Project.push_source: PushSource | None = None`; `Project.sources -> tuple[Source, ...]` (files, then the push source if any); `make_project(..., push_source=None)`.

- [ ] **Step 1: Write failing tests** in `tests/test_push_source.py` using `make_project_folder` + `scan_projects`:
  - `test_push_token_env_adds_push_source`: yaml `WSO2_YAML + "push_token_env: ACME_TOKEN\n"` gives `project.push_source.id == "demo/push"`, `.token_env == "ACME_TOKEN"`, `.timezone == "Asia/Kolkata"`, and `project.sources[-1] is project.push_source`.
  - `test_project_without_push_has_no_push_source`: `push_source is None`, `sources == files`.
  - `test_bad_push_token_env_marks_project_invalid`: values `"lower"`, `"1BAD"`, `"A" * 65`, `123` each give `project.error` containing `push_token_env`.
  - `test_file_named_push_is_skipped`: with push enabled and a `push.log` in the folder, `push.log` appears in `project.skipped` and not in `project.files`.
- [ ] **Step 2: Run** `pytest tests/test_push_source.py -v`. Expected: FAIL (unknown key `push_token_env`).
- [ ] **Step 3: Implement.** `PushSource` in `sources.py`; add `"push_token_env"` to `KEYS`; validate in `_parse` (raise `ConfigError(f"{where}: push_token_env must match ...")`); build `PushSource(id=f"{folder.name}/push", token_env=..., **settings)` with the same `settings` dict the files get; in `_files` skip stem `push` when push is enabled with reason "the id 'push' is reserved for pushed logs". Add `push_source` and `sources` to `Project`, and `push_source` to `make_project`.
- [ ] **Step 4: Run** the new tests and `pytest tests -k "project" -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: declare a push source in project.yaml`.

### Task 2: Pushed events are visible to search, the agent and coverage

**Files:**
- Modify: `app/ingestion/coverage.py`, `app/controllers/projects.py`, `app/controllers/dependencies.py`, `app/agent/prompt.py`, `app/agent/tools/find.py`, `app/agent/tools/overview.py`, `app/agent/tools/search_logs.py`
- Test: `tests/test_push_source.py` (append)

**Interfaces:**
- Consumes: `Project.sources`, `PushSource` (Task 1).
- Produces: `coverage_for_all` returns `SourceCoverage(status="complete", indexed_through=None)` for a source with no `path`; `orphan_ids` treats `project.sources` ids as known; `GET /projects/{id}/files` includes a row named `push` for a push project.

- [ ] **Step 1: Write failing tests** (integration, test DB):
  - `test_coverage_of_push_source_is_complete`: `coverage_for_all(session, [push_source])` returns one item, status `complete`.
  - `test_push_id_is_not_an_orphan`: insert a `source_index_state` row for `demo/push`; `orphan_ids(session, project)` is `[]`.
  - `test_files_listing_has_push_row`: via `projects_client`, project with a push source and one stored event under `demo/push`; `GET /api/v1/projects/demo/files` has a row with `name == "push"`, `event_count == 1`.
  - `test_agent_prompt_counts_push_only_project_as_not_empty`: `build_system_prompt(make_project(push_source=...))` does not contain "no log files yet".
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** In `coverage.py` `_stat` and `coverage_for_all`: sources without a `path` attribute skip the stat and get the `complete` coverage. Replace `project.files` with `project.sources` in `orphan_ids`, `dependencies.get_agent` (`time_spans`), `projects._summary` (spans only; `file_count` stays `len(project.files)`), `prompt._span` and the empty-project check (the "N files" count stays files), and `sources = project.sources` in `find.py`, `overview.py`, `search_logs.py`. In `list_files`, keep the file rows as they are and append one row for the push source: `_file_row(push.id, "push", "complete", None, stats.get(push.id), None, None)`, with `push.id` included in the `file_stats` id list.
- [ ] **Step 4: Run** the new tests plus `pytest tests -q -x`. Expected: all PASS (existing file-only behaviour unchanged).
- [ ] **Step 5: Commit** `feat: search, agent and coverage include the push source`.

### Task 3: Insert-only store with dedup

**Files:**
- Modify: `app/ingestion/store.py`, `app/ingestion/pipeline.py`
- Test: `tests/test_store.py` (append)

**Interfaces:**
- Produces: `insert_new_events(session: AsyncSession, source_id: str, rows: Sequence[EventRow]) -> int` (number of rows actually inserted; never commits); `prepare_event(event: SourceEvent, source: SourceBase) -> EventRow` in `pipeline.py` (rename of `_prepare`, same body, updated call site and any tests that import `_prepare`).

- [ ] **Step 1: Write failing tests** (reuse `row`, `write`, `count` helpers; `pytestmark = integration`):
  - `test_insert_new_events_skips_existing_keys`: insert `row(1)`, then insert `[row(1), row(2)]`; second call returns 1; `count == 2`.
  - `test_insert_new_events_does_not_overwrite`: insert `row(1, "first")`, then `row(1, "second")`; stored `raw_text == "first"`.
  - `test_duplicates_inside_one_call_count_once`: `[row(3), row(3)]` returns 1.
  - `test_concurrent_identical_inserts_leave_one_row`: `asyncio.gather` of two `write`-style calls of `insert_new_events` with `row(4)`; `count == 1`, returned counts sum to 1.
  - `test_empty_rows_return_zero`.
- [ ] **Step 2: Run** `pytest tests/test_store.py -v`. Expected: FAIL (name not defined).
- [ ] **Step 3: Implement** `insert_new_events` with `insert(LogEvent).on_conflict_do_nothing(index_elements=[LogEvent.source_id, LogEvent.event_key]).returning(LogEvent.id)` executed with `_values(...)` rows; return the number of returned ids. Verify with the tests that executemany + RETURNING reports only inserted rows; if it does not on the installed SQLAlchemy, execute one multi-row `values(...)` statement instead. Rename `_prepare` to `prepare_event` and widen the type hint to `SourceBase`.
- [ ] **Step 4: Run** `pytest tests/test_store.py tests/test_pipeline.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: insert-only store with dedup for pushed events`.

### Task 4: Ingest endpoint

**Files:**
- Create: `app/ingestion/push.py`, `app/schemas/ingest.py`, `app/controllers/ingest.py`
- Modify: `app/controllers/router.py` (add `ingest` to the module tuple), `app/core/errors.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `Project.push_source`, `insert_new_events`, `prepare_event`, `find_project`, `get_usable_project`-style 409.
- Produces:
  - `errors.py`: `UnauthorizedError` (401, `unauthorized`), `PayloadTooLargeError` (413, `payload_too_large`), `InvalidPayloadError` (422, `validation_error`), `StorageUnavailableError` (503, `storage_unavailable`).
  - `schemas/ingest.py`: `PushEvent(message: str, timestamp: str | None = None, level: str | None = None)` (extra keys ignored); `IngestResponse(received: int, stored: int, duplicates: int)`.
  - `push.py`: `push_event_key(redacted_text: str, sender_timestamp: str | None) -> str`; `build_push_rows(events: Sequence[PushEvent], source: PushSource) -> list[EventRow]`; `check_token(source: PushSource, header: str | None) -> None` (raises `NotFoundError(NOT_FOUND)` when the env var is unset/empty, `UnauthorizedError` when missing or wrong); `parse_body(raw: bytes, content_type: str) -> list[PushEvent]` (raises `PayloadTooLargeError` over 1 MiB or 1000 events, `InvalidPayloadError` otherwise).
  - Route `POST /api/v1/projects/{project_id}/ingest -> IngestResponse`.

- [ ] **Step 1: Write failing tests** in `tests/test_ingest.py` (integration; `projects_client` with a project built via `make_project(push_source=PushSource(id="demo/push", token_env="DEMO_TOKEN", timezone="UTC"))`; `monkeypatch.setenv("DEMO_TOKEN", "s3cret")`; helper `push(http, body, token="s3cret", **kw)`):
  - `test_push_stores_events`: two events return `{"received": 2, "stored": 2, "duplicates": 0}`; two rows under `demo/push` with `embedding IS NULL`, `locator == {"kind": "push"}`.
  - `test_same_batch_twice_is_deduplicated`: second reply `stored == 0, duplicates == 2`; row count 2.
  - `test_duplicate_inside_one_batch`: `[e, e]` gives `stored 1, duplicates 1`.
  - `test_secret_is_redacted_and_deduplicates`: two messages `"login failed password=hunter2"` and `"... password=other"` (use a secret shape the existing `redact` catches; check `tests/test_redaction*.py`) store one row whose `raw_text` has neither secret.
  - `test_auth`: no header, wrong token, wrong scheme give 401; right token 200.
  - `test_not_found_cases_look_the_same`: unknown project, project without push, unset env var all give 404 with identical bodies.
  - `test_invalid_project_is_409`: `make_project(error="bad", push_source=...)` gives 409.
  - `test_too_many_events_and_oversize_body_are_413`: 1001 events; a body over 1 MiB.
  - `test_malformed_bodies_are_422`: invalid JSON, non-UTF-8 bytes, a JSON object instead of a list, an event without `message`, `message` of `""` and of `"   "`; nothing stored.
  - `test_ndjson_body`: `Content-Type: application/x-ndjson` with two lines stores two rows; a bad line is 422.
  - `test_timestamps`: ISO `"2026-11-04T10:00:00Z"` gives `occurred_at` 10:00 UTC; naive `"2026-11-04T10:00:00"` in a project with timezone `Asia/Kolkata` gives 04:30 UTC; `"not a time"` returns 200 and `occurred_at` is whatever the parser finds in the text (or NULL).
  - `test_sender_level_used_only_when_parser_finds_none`: message `"plain text"` with `level: "warn"` stores `WARN`; message with a parsed level keeps the parsed one.
  - `test_database_down_is_503`: override `get_session` with one whose `execute` raises `OperationalError`; reply 503, body without log content.
  - `test_concurrent_identical_pushes_store_one_row`: `asyncio.gather` of two identical requests; one row, `stored` values sum to 1.
- [ ] **Step 2: Run** `pytest tests/test_ingest.py -v`. Expected: FAIL (404 for the route).
- [ ] **Step 3: Implement** the interfaces above. Route order: `find_project` (404) -> `project.error` (409) -> `project.push_source` present (else 404) -> `check_token` -> read body (reject by `Content-Length` early, and by actual length) -> `parse_body` -> `build_push_rows` -> `async with session.begin(): stored = await insert_new_events(...)`; wrap `SQLAlchemyError` into `StorageUnavailableError` (log class name only). `build_push_rows`: `redact` the message, `RawEvent`/`SourceEvent` wrapped for `prepare_event` with `event_key = push_event_key(...)`; apply a parsed sender `timestamp` (`datetime.fromisoformat`, naive read in `ZoneInfo(source.timezone)`, stored as UTC) as `occurred_at` via `dataclasses.replace`; apply sender `level.upper()` only when the row's level is None. Reject empty/whitespace `message` in `parse_body` as 422.
- [ ] **Step 4: Run** `pytest tests/test_ingest.py -q`, then `pytest tests -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: POST /projects/{id}/ingest with content-hash dedup`.

### Task 5: Continuous embedding loop

**Files:**
- Create: `app/ingestion/embed_loop.py`
- Modify: `app/main.py`, `app/core/config.py`
- Test: `tests/test_embed_loop.py`

**Interfaces:**
- Consumes: `reembed_missing(session_factory, source_id=None, *, embed, chunk)` from `pipeline.py`.
- Produces: `async def run_embed_loop(session_factory: SessionFactory, *, interval: float, embed: Embedder = embed_documents) -> None` (runs until cancelled; each pass calls `reembed_missing`, then sleeps `interval`; any `Exception` is logged by class name and the loop continues; `CancelledError` propagates). `Settings.push_embed_interval_seconds: float = 5.0`.

- [ ] **Step 1: Write failing tests** (integration):
  - `test_loop_fills_missing_vectors`: store `row(1)` with `embedding=None` through `insert_new_events`; run the loop as a task with `interval=0.01` and `embed=lambda texts: [[0.1] * EMBEDDING_DIMENSIONS for _ in texts]`; poll up to 2 s until the row has a vector; cancel the task and await it.
  - `test_loop_survives_a_failing_pass`: monkeypatch `app.ingestion.embed_loop.reembed_missing` to raise `RuntimeError` on the first call and succeed after; the second call happens (counter reaches 2 within 2 s).
  - `test_loop_picks_up_rows_pushed_later`: start the loop on an empty table, then insert a row; it gets a vector.
  - `test_loop_stops_on_cancel`: cancel raises `CancelledError` when awaited and finishes within 1 s.
- [ ] **Step 2: Run** `pytest tests/test_embed_loop.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `run_embed_loop`. In `lifespan` (`app/main.py`): `task = asyncio.create_task(run_embed_loop(SessionLocal, interval=get_settings().push_embed_interval_seconds))`; after `yield`, cancel it, `await` it with `contextlib.suppress(asyncio.CancelledError)`, then `engine.dispose()`.
- [ ] **Step 4: Run** `pytest tests -q`. Expected: all PASS. Then a manual check: start the app (`uvicorn app.main:app`), `POST` two identical events to a project with `push_token_env` set, confirm `stored: 1, duplicates: 1`, and that the row has an embedding within ~`PUSH_EMBED_INTERVAL_SECONDS` seconds.
- [ ] **Step 5: Commit** `feat: background loop embeds pushed events`.

### Task 6: Regression check and docs

**Files:**
- Modify: `README.md` (or the existing docs index; one short section)

- [ ] **Step 1: Document** how to enable push: the `push_token_env` key, the env var, a `curl` example with `Authorization: Bearer`, the reply shape, the limits, and "deploy behind TLS".
- [ ] **Step 2: Run** `pytest tests --cov=app -q`; new code must be at or above 80% coverage.
- [ ] **Step 3: Agent regression.** The agent tools now read `project.sources`. Ask the user to re-run their golden eval (`scripts/eval_agent.py`, uses the Groq key) and compare with `tests/eval-result-v4.json`. Do not run it unprompted.
- [ ] **Step 4: Commit** `docs: how to push logs`.
