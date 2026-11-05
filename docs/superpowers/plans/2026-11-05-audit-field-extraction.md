# Audit Field Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WSO2 `AUDIT_LOG` events store action, result, actor, target, user and application as fields, and e-mail masks carry a stable pseudonym so hidden users can be told apart.

**Architecture:** A pure `audit_fields(message)` function runs inside the WSO2 text layout and merges `audit_*` keys into the `fields` JSONB that already exists. The e-mail redaction rule gains an optional keyed code (`[REDACTED:email:u<11 hex>]`); signatures strip the code so event kinds do not split. A backfill script recomputes `fields` for stored rows.

**Tech Stack:** Python 3.13, `re`, `hmac`, FastAPI settings (pydantic-settings), SQLAlchemy async, pytest (`integration` mark for DB tests).

**Spec:** `docs/superpowers/specs/2026-11-05-audit-field-extraction-design.md`

## Global Constraints

- Field keys, exactly: `audit_action`, `audit_result`, `audit_actor`, `audit_target`, `audit_user`, `audit_application`. A key is omitted when its value is missing or empty. Nothing else from Data is stored (never `Claims`).
- Each extracted value is at most 256 characters; the scan covers at most the first 16 KiB of the message; the first match of a key wins, except `audit_result` which takes the last `Result`/`Outcome` match.
- A leading `<NAME>/` user-store prefix (e.g. `PRIMARY/`) is removed from `audit_actor` and `audit_user`.
- Pseudonym code = `u` + first 11 hex characters of `HMAC-SHA256(key, lower-cased address)`; mask form `[REDACTED:email:u1a2b3c4d5e6]`. Setting `PSEUDONYM_KEY` (`Settings.pseudonym_key: str | None = None`). Without a key the mask stays `[REDACTED:email]` and one warning is logged at app start.
- Redaction stays idempotent; the key and the addresses are never logged; `kinds` still reports `email`.
- `audit_fields` never raises; a line that matches nothing gives `{}`.
- Signatures: `[REDACTED:<kind>:<code>]` is reduced to `[REDACTED:<kind>]` before the template is built; signatures of existing rows do not change.
- No schema changes. Coverage target 80% for new code. Run tests with `.venv/Scripts/python -m pytest tests -q --ignore=tests/test_agent_graph.py` (that file has 18 older failures).

## Review Focus

- A huge or hostile audit line (1 MiB of repeated `Action=`, long unterminated quotes) is handled in well under a second with bounded values (Task 1).
- The push path redacts twice; with a key set the second pass must change nothing (Task 2).
- A pseudonym code is never rewritten by the card rule, whatever the address (Task 2).
- No key set: plain `[REDACTED:email]`, no crash, one warning at start (Tasks 2 and 3).
- Backfill run twice: the second run changes nothing, and embedding and signature columns are untouched (Task 4).

## File Structure

- Create `app/ingestion/fields/audit.py`: `audit_fields`.
- Modify `app/ingestion/fields/text_layouts.py`: call it for `wso2_carbon` + `AUDIT_LOG`.
- Create `app/ingestion/pseudonym.py`: `email_code`, `configured_pseudonym_key`.
- Modify `app/ingestion/redaction.py`: keyed e-mail mask; `_ALREADY_MASKED`; `redact(..., pseudonym_key=None)`.
- Modify `app/core/config.py`: `pseudonym_key`.
- Modify `app/ingestion/signature.py`: strip codes.
- Modify `app/ingestion/pipeline.py`, `app/ingestion/push.py`, `app/main.py`: pass the key; warn at start.
- Create `scripts/backfill_fields.py`.
- Modify `README.md`, `.env.example`.
- Tests: `tests/test_audit_fields.py`, `tests/test_pseudonym.py`, `tests/test_backfill_fields.py`; append to `tests/test_signature.py` and `tests/test_ingest.py`.

---

### Task 1: `audit_fields` and the layout hook

**Files:**
- Create: `app/ingestion/fields/audit.py`
- Modify: `app/ingestion/fields/text_layouts.py` (`resolve_text_event`)
- Test: `tests/test_audit_fields.py`

**Interfaces:**
- Produces: `audit_fields(message: str) -> dict[str, str]` returning only the `audit_*` keys listed in Global Constraints. After this task, `resolve(text, source).fields` for a WSO2 `AUDIT_LOG` event contains those keys next to `tenant`/`app`.

- [ ] **Step 1: Write failing tests** in `tests/test_audit_fields.py`. Constants (real lines, message part only):
  - `PIPE_LOGIN = 'Initiator : [REDACTED:email] | Action : Login | Target : ApplicationAuthenticationFramework | Data : { "ContextIdentifier" : "1d340c53","AuthenticatedUser" : "[REDACTED:email]","ServiceProviderName" : "Console","RequestType" : "oidc","UserStoreDomain" : "PRIMARY" } | Result : Success'`
  - `PIPE_FAILED = 'Initiator : Goutam003 | Action : Login | Target : ApplicationAuthenticationFramework | Data : { "ServiceProviderName" : "portalApplication","StepNo" : "1" } | Result : Failed'`
  - `EQUALS_CLAIMS = 'Initiator=wso2.system.user Action=Get-User-Claim-Values Target=admin Data={"ServiceProviderName":"Console","Claims":{"http://wso2.org/claims/username":"admin","http://wso2.org/claims/roles":"Internal/admin"}} Outcome=Success'`
  - `UPDATE_SESSION = 'Initiator : PRIMARY/admin | Action : UpdateSession | Data : { {"traceId":"17deb80d","AuthenticatedUser":"PRIMARY/admin"} } | Result : Success'`
  Tests: `test_pipe_style_login` expects `{"audit_action": "Login", "audit_result": "Success", "audit_actor": "[REDACTED:email]", "audit_target": "ApplicationAuthenticationFramework", "audit_user": "[REDACTED:email]", "audit_application": "Console"}`; `test_failed_login_has_failed_result_and_no_user` expects result `Failed`, actor `Goutam003`, application `portalApplication`, and no `audit_user`; `test_equals_style_takes_outcome_and_ignores_claims` expects action `Get-User-Claim-Values`, result `Success`, actor `wso2.system.user`, target `admin`, application `Console`, and no key mentioning claims or roles; `test_user_store_prefix_is_removed` expects `UPDATE_SESSION` to give actor `admin`, user `admin`, and no `audit_application`/`audit_target`; `test_a_message_without_audit_fields_gives_an_empty_dict` for `"Server started"`; `test_values_are_capped_at_256_characters` with a 5000-character `Initiator`; `test_hostile_input_is_fast_and_bounded` with `"Action=" * 200000` and with `'"ServiceProviderName" : "' + "a" * 10**6` (each under 1 s via `time.perf_counter`, every value at most 256 characters); `test_the_last_result_wins` for a line with `Result : x` inside Data and `Result : Success` at the end. Layout tests via `resolve(text, FileSource-like TEXT source)` as in `tests/test_field_resolver.py`: a full `TID: [-1234] [] [2026-11-05 16:03:26,686] [f39cb36f]  INFO {AUDIT_LOG} - <PIPE_LOGIN>` line gives `fields["audit_application"] == "Console"` and keeps `tenant == "-1234"`; the same message under `{OtherLogger}` gives no `audit_*` key; a Spring Boot line containing `Action=Login` gives none.
- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest tests/test_audit_fields.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `audit_fields` with one precompiled bounded regex per key (patterns in the spec, section 3: header keys `Action`, `Result|Outcome`, `Initiator`, `Target` take `[:=]` then a value that is either a whole `[REDACTED:...]` mask or a run of characters up to whitespace or `|`; `"AuthenticatedUser"` and `"ServiceProviderName"` take the first `"…"` string value after `:`), applied to `message[:16384]`; strip the user-store prefix with `^[A-Za-z][A-Za-z0-9_.-]{0,31}/(?=.)`; wrap in a try that returns `{}` on any exception. In `resolve_text_event`, after the layout match, when `layout.name == "wso2_carbon"` and the layout's logger is `AUDIT_LOG`, return `layout.fields | audit_fields(message)` as the second value.
- [ ] **Step 4: Run** the new tests and `.venv/Scripts/python -m pytest tests -q --ignore=tests/test_agent_graph.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: extract audit fields from WSO2 AUDIT_LOG events`.

### Task 2: Keyed e-mail pseudonym in redaction

**Files:**
- Create: `app/ingestion/pseudonym.py`
- Modify: `app/ingestion/redaction.py`, `app/core/config.py`
- Test: `tests/test_pseudonym.py`

**Interfaces:**
- Produces: `Settings.pseudonym_key: str | None = None`; `email_code(key: bytes, address: str) -> str` (`"u"` + 11 hex, case-insensitive on `address`); `configured_pseudonym_key() -> bytes | None` (the encoded setting, or `None` when unset or blank); `redact(text: str, *, pseudonym_key: bytes | None = None) -> RedactionResult`, unchanged when the key is `None`.

- [ ] **Step 1: Write failing tests** in `tests/test_pseudonym.py`: `test_same_address_same_code_case_insensitive` (`Bob@X.com` and `bob@x.com`); `test_different_addresses_different_codes`; `test_different_keys_different_codes`; `test_redact_without_key_keeps_the_plain_mask` (`redact("mail bob@x.com").text == "mail [REDACTED:email]"`); `test_redact_with_key_adds_the_code` (text matches `mail \[REDACTED:email:u[0-9a-f]{11}\]`, address absent, `"email" in kinds`); `test_redaction_is_idempotent_with_a_key` (`redact(redact(t, key).text, key).text == redact(t, key).text`, also for `password=[REDACTED:secret]` neighbours); `test_the_code_is_never_rewritten_by_the_card_rule` (200 generated addresses, each result fully matches the mask pattern); `test_masks_with_a_code_survive_the_secret_scanner` (`password=[REDACTED:email:u1a2b3c4d5e6]` is left as is); `test_configured_pseudonym_key_reads_the_setting` (monkeypatch `app.ingestion.pseudonym.get_settings` to a `Settings(_env_file=None, database_url="postgresql://u@h/db", pseudonym_key="k")` -> `b"k"`; unset or `"  "` -> `None`).
- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest tests/test_pseudonym.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** `email_code` with `hmac.new(key, address.lower().encode(), "sha256").hexdigest()[:11]`. In `redaction.py` make `_ALREADY_MASKED` accept an optional `:u[0-9a-f]{11}` before the closing bracket, build the after-pairs list per call so the e-mail rule's replacement returns `[REDACTED:email:<code>]` when a key is given, and keep `_bare_end` as is (it already treats `[REDACTED:` … `]` as one unit). Add the setting with a comment naming `PSEUDONYM_KEY`.
- [ ] **Step 4: Run** the new tests and the existing `tests/test_redaction*.py`. Expected: PASS, no existing redaction test changes.
- [ ] **Step 5: Commit** `feat: stable e-mail pseudonyms in redaction when a key is set`.

### Task 3: Signatures, wiring and the start-up warning

**Files:**
- Modify: `app/ingestion/signature.py`, `app/ingestion/pipeline.py` (`prepare_event`), `app/ingestion/push.py` (`_row`), `app/main.py` (`lifespan`)
- Test: `tests/test_signature.py` (append), `tests/test_ingest.py` (append)

**Interfaces:**
- Consumes: `redact(..., pseudonym_key=)`, `configured_pseudonym_key()` from Task 2; the `audit_*` fields from Task 1.
- Produces: signatures that ignore the code; stored `raw_text` and `fields` carrying coded masks when a key is set.

- [ ] **Step 1: Write failing tests.** Signature: `test_users_with_different_codes_share_a_signature` (two messages differing only in `[REDACTED:email:u111111111111]` vs `...u222222222222` have equal `sig` and `template`, template contains `[REDACTED:email]`); `test_a_plain_mask_signature_is_unchanged` (existing behaviour). Ingest (integration, `projects_client`, monkeypatch `app.ingestion.pseudonym.get_settings` or the function `configured_pseudonym_key` in both `app.ingestion.pipeline` and `app.ingestion.push` to return `b"test-key"`): `test_pushed_audit_line_stores_coded_fields` pushes the pipe login line wrapped in a WSO2 header with `Initiator : a@x.com` and `"AuthenticatedUser" : "a@x.com"`; stored `raw_text` has no `a@x.com`, `fields["audit_actor"]` equals `fields["audit_user"]` and matches `\[REDACTED:email:u[0-9a-f]{11}\]`; `test_two_users_one_signature_two_codes` pushes the same line for `a@x.com` and `b@x.com`: two rows, equal `signature`, different `audit_user`; `test_without_a_key_the_plain_mask_is_stored` (patch to `None`); `test_lifespan_warns_once_when_the_key_is_missing` (caplog on start with the setting unset, no warning when set).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** In `signature._template` reduce coded masks first with a precompiled `\[REDACTED:([a-z_]+):u[0-9a-f]{11}\]` -> `[REDACTED:\1]`. Pass `pseudonym_key=configured_pseudonym_key()` in `prepare_event` and `_row`. In `lifespan`, call `configured_pseudonym_key()` once and log a single warning (no key material) when it returns `None`.
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat: pseudonym-aware signatures and ingestion wiring`.

### Task 4: Backfill script

**Files:**
- Create: `scripts/backfill_fields.py`
- Test: `tests/test_backfill_fields.py`

**Interfaces:**
- Produces: `async def backfill(session_factory, sources: Sequence[Source], batch_size: int = 500) -> int` (events changed) and a CLI `python -m scripts.backfill_fields [--project ID] [--logs-root PATH]` that iterates `project.sources` (files and the push source) of valid projects.

- [ ] **Step 1: Write failing tests** (integration, as `tests/test_backfill_signatures.py`): `test_backfill_fills_audit_fields_of_stored_rows` stores a row whose `raw_text` is a WSO2 audit line with `fields={}`, runs `backfill`, expects the `audit_*` keys and a return of 1; `test_second_run_changes_nothing` (returns 0); `test_other_columns_are_untouched` (embedding, signature, semantic_text, raw_text equal before and after); `test_rows_of_other_sources_are_untouched`; `test_push_source_rows_are_included` via a `PushSource`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** by copying the keyset-batching shape of `scripts/backfill_signatures.py`: select `id, raw_text, fields` for the source, compute `dict(resolve(row.raw_text, source).fields)`, write only rows where it differs, update by primary key.
- [ ] **Step 4: Run** the tests and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat: backfill_fields script`.

### Task 5: Docs and final checks

**Files:**
- Modify: `README.md`, `.env.example`

- [ ] **Step 1: Document** in `README.md` a short "Audit fields and user pseudonyms" section: the six `audit_*` keys, the `PSEUDONYM_KEY` setting (what it does, keep it secret, changing it changes every code, rows stored earlier keep the plain mask), how to create a key (`python -c "import secrets; print(secrets.token_urlsafe(32))"`), and `python -m scripts.backfill_fields --project <id>`. Add `PSEUDONYM_KEY=` (empty) with a comment to `.env.example`.
- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest tests --ignore=tests/test_agent_graph.py -q --cov=app.ingestion.fields.audit --cov=app.ingestion.pseudonym --cov=app.ingestion.redaction --cov-report=term-missing`. Expected: PASS; new modules at least 80%.
- [ ] **Step 3: Run the backfill against the dev database** for `sagar-wso2` and confirm with a count query that `fields->>'audit_action'` is set for the audit rows (about 374). Note: this writes to the dev database, so do it only after the user agrees.
- [ ] **Step 4: Agent regression.** Signatures and text changed for new rows only; ask the user to re-run `scripts/eval_agent.py` and compare with `tests/eval-result-v4.json`. Do not run it unprompted.
- [ ] **Step 5: Commit** `docs: audit fields and pseudonym key`.
