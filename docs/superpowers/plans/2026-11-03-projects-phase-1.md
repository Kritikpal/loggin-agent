# Projects (phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat `sources.yaml` allowlist with project folders, scope chat, search and the agent's tools to
one project, and add a read-only projects API plus a project picker in the chat UI.

**Architecture:** A scanner turns `LOGS_ROOT/<project>/project.yaml` + the files beside it into immutable `Project`
values holding today's `FileSource` objects, so the indexer, search and coverage code keep working on sources. File ids
become `<project>/<stem>`. Every request scans the folders (as `sources.yaml` is read per request today), and the
agent's tools are built with only the chosen project's files.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async / PostgreSQL + pgvector / LangChain agent on Groq; React 19 +
Radix Themes + Vite frontend.

**Spec:** `docs/superpowers/specs/2026-11-03-projects-design.md`. Read it first. Section numbers below (§) refer to it.

## Global Constraints

- Project id and file stem pattern: `[a-z0-9][a-z0-9_.-]{0,63}`. Full file id: `<project_id>/<stem>`, where `stem` = `Path(name).stem.lower()`.
- `project.yaml` keys: `title` (required, 1-120 chars), `description` (optional, <= 300 chars), `type` (required, a preset),
  `timezone` (default `UTC`), `include` (glob, default `*.log*`), and the overrides `event_start`, `app_packages`, `field_mappings`. Any other key is an error.
- Presets: `wso2` (text, `^TID: \[`, `[org.wso2]`), `spring-boot` (text, default event start, `[]`), `logback` (text, default, `[]`), `json` (json, n/a, `[]`).
- An unknown or malformed `project_id` returns 404 with the constant message `"No such project."`. The id is never joined into a path.
- File statuses: `not_indexed`, `error`, `stale`, `partial`, `complete` (from `coverage.py`), plus `missing` and `skipped`.
- Agent coverage object lists at most 10 file names in total, plus a count of the rest.
- `summarize_logs` keeps `MAX_GROUPS = 30`. Rows are ordered by count, highest first.
- No schema migration. No change to search ranking, filters or the hybrid query.
- Commit messages: `<type>: <description>` (conventional commits). No attribution lines.

## Review Focus

1. **A project id containing `_` or `%`.** The "missing files" lookup matches ids by prefix. A SQL `LIKE 'wso2_gw/%'`
   would also match `wso2xgw/...`, so another project's events would show as this project's missing files. Test in Task 2.
2. **File names that only differ by case** (`App.log` and `app.log` on Linux) or that contain spaces or `+`. Both
   files must be `skipped` with a reason, and the rest of the project must still work. Test in Task 1.
3. **A `project.yaml` that is empty, a list, has a BOM, or is not UTF-8.** The project shows as `invalid`; nothing
   crashes and the other projects still load. Test in Task 1.
4. **The model passes a full id (`wso2-gateway/x`) or another project's file name in `files`.** It gets the "unknown
   file" error listing this project's short names, never a doubled prefix or a match in another project. Test in Task 4.
5. **A valid project with zero files or nothing indexed.** Chat still answers, the prompt says there are no events, and
   the coverage object reads `files: 0`. Tests in Tasks 4 and 5.

---

## File structure

| File | Responsibility |
|---|---|
| `app/ingestion/projects.py` (new) | `Project`, `SkippedFile`, `scan_projects`, `find_project`; `project.yaml` validation; file id rules |
| `app/ingestion/fields/text_layouts.py` | Add `TYPE_PRESETS` |
| `app/ingestion/sources.py` | Keep `SourceBase`/`FileSource`/`get_source`/`ConfigError` and the field validators; drop the YAML loader (Task 6) |
| `app/ingestion/coverage.py` | Add `file_stats` and `orphan_ids` |
| `app/schemas/projects.py` (new) | API response models |
| `app/controllers/projects.py` (new) | `/projects` routes, including chat and search-logs |
| `app/controllers/dependencies.py` | `get_projects`, `get_project`, `get_chat_project`, `get_agent(project)` |
| `app/controllers/{health,tools,chat}.py`, `router.py` | Remove `/sources`, `/tools/search-logs`, `/ai/chat`; register projects router |
| `app/agent/tools.py`, `app/tools/search_queries.py` | Project scoping, short ids, `files`, coverage object, `group_by` |
| `app/agent/graph.py`, `app/agent/prompt.py` | `build_agent(project=…)`, project header and type hints, new coverage note |
| `scripts/index_logs.py`, `scripts/eval_agent.py` | Scan projects instead of `sources.yaml` |
| `tests/project_helpers.py` (new) | `make_project(...)` used by the agent and API tests |
| `frontend/src/api.ts`, `frontend/src/App.tsx` | `listProjects`, `sendChat(projectId, …)`, project picker |

---

### Task 1: Scan project folders

**Files:**
- Create: `app/ingestion/projects.py`
- Modify: `app/ingestion/fields/text_layouts.py` (add `TYPE_PRESETS`), `app/ingestion/sources.py` (make `_regex`,
  `_timezone`, `_packages`, `_mappings` public as `parse_regex`, `parse_timezone`, `parse_packages`, `parse_mappings`;
  `SOURCE_ID` becomes the two-part pattern), `app/core/config.py` (add `logs_root: Path = ROOT_DIR / "logs"`, env `LOGS_ROOT`)
- Test: `tests/test_projects.py`

**Interfaces:**
- Produces:
  - `TYPE_PRESETS: Mapping[str, Preset]` in `text_layouts.py`, where `Preset` is a frozen dataclass with fields
    `format: Literal["text","json"]`, `event_start: str | None`, `app_packages: tuple[str, ...]`.
  - `PROJECT_ID: re.Pattern` in `projects.py`.
  - `@dataclass(frozen=True) class SkippedFile: name: str; reason: str`
  - `@dataclass(frozen=True, kw_only=True) class Project:` with fields `id: str`, `title: str`, `description: str`, `type: str`,
    `timezone: str`, `include: str`, `folder: Path`, `files: tuple[FileSource, ...]`, `skipped: tuple[SkippedFile, ...]`,
    `error: str | None`, and the property `status -> Literal["ok","invalid"]` (`"invalid"` iff `error`). An invalid project has
    `files=()`, `title=id` and `type=""`.
  - `scan_projects(root: Path) -> tuple[Project, ...]`, sorted by id. A missing root logs a warning and returns `()`.
  - `find_project(projects: tuple[Project, ...], project_id: str) -> Project`, which raises
    `NotFoundError("No such project.")` for an unknown id, including malformed ones.

- [ ] **Step 1: Write the failing tests** in `tests/test_projects.py`, using `tmp_path` folders written by a helper
  `write_project(root, name, yaml_text, *files)`:
  - `test_a_wso2_project_loads_with_its_preset`: `type: wso2`. Then `files[0].event_start.pattern == r"^TID: \["`,
    `files[0].app_packages == ("org.wso2",)`, `files[0].timezone == "Asia/Kolkata"` (from yaml), and
    `files[0].id == "wso2-gateway/wso2carbon-10-30-2026.1"` for `wso2carbon-10-30-2026.1.log`.
  - `test_overrides_beat_the_preset`: `event_start: '^X'` gives `pattern == "^X"`.
  - `test_include_glob_filters_files_and_project_yaml_is_never_a_file`: default `*.log*` skips `notes.txt` and
    `project.yaml`. Hidden `.x.log` is ignored. A subfolder `prod/a.log` is ignored.
  - `test_folder_without_project_yaml_is_not_a_project`.
  - `test_bad_yaml_marks_only_that_project_invalid`, parametrized over `""`, `"- a\n- b"`, `"title: x\ntype: nope"`,
    `"title: x\ntype: wso2\ncolour: red"`, `"title: x\ntype: wso2\ntimezone: Mars/Base"`, a `description` of 301 chars,
    and raw bytes `b"\xff\xfe"`. Each gives `status == "invalid"`, a non-empty `error`, `files == ()`, and a second valid
    project in the same root still has `status == "ok"`. A UTF-8 BOM before valid YAML loads as `ok` (read with `utf-8-sig`).
  - `test_bad_folder_name_is_skipped`: folder `Bad Name/` is not returned, and a warning is logged.
  - `test_unusable_file_names_are_skipped_with_a_reason`: `my app.log` gives a `SkippedFile` whose reason mentions the
    name pattern. `A.log` and `a.log` (create both; skip the test with `pytest.skip` if the filesystem is
    case-insensitive) are both skipped with a reason mentioning the duplicate id. The other files are still in `files`.
  - `test_symlink_outside_the_folder_is_skipped`: `pytest.skip` when `os.symlink` raises `OSError` (Windows without privilege).
  - `test_find_project_has_one_message_for_unknown_and_malformed`: `"nope"`, `"../etc"`, `"UPPER"` all raise
    `NotFoundError` with `.message == "No such project."`.
  - `test_missing_root_is_no_projects`.

- [ ] **Step 2: Run them and see them fail**

  Run: `.venv/Scripts/python -m pytest tests/test_projects.py -q`
  Expected: collection error, `No module named 'app.ingestion.projects'`.

- [ ] **Step 3: Implement** `TYPE_PRESETS`, the `sources.py` renames (update the `_parse_source` call sites so
  `tests/test_sources.py` still passes), `Settings.logs_root`, and `projects.py`.
  - Each project's errors are caught as `ConfigError` (plus `UnicodeDecodeError` and `yaml.YAMLError`), so one project
    never raises out of `scan_projects`.
  - The symlink check: `file.resolve().is_relative_to(folder.resolve())`.
  - Files are matched with `folder.glob(include)` and filtered to `is_file()`. Never recurse.

- [ ] **Step 4: Run the new and old source tests**

  Run: `.venv/Scripts/python -m pytest tests/test_projects.py tests/test_sources.py -q`
  Expected: all pass (some skipped on Windows).

- [ ] **Step 5: Commit**: `git commit -m "feat: scan project folders into projects with preset parsing settings"`

---

### Task 2: File stats and missing files

**Files:**
- Modify: `app/ingestion/coverage.py`
- Test: `tests/test_project_coverage.py` (marked `integration`, uses the `session` fixture from `tests/conftest.py`)

**Interfaces:**
- Consumes: `Project` (Task 1).
- Produces:
  - `@dataclass(frozen=True) class FileStats: event_count: int; first_at: datetime | None; last_at: datetime | None; parsed_ratio: float | None`
  - `async def file_stats(session, source_ids: Sequence[str]) -> dict[str, FileStats]`. An id with no events is
    absent from the result. `parsed_ratio` = events with both `level` and `occurred_at` set, divided by all events.
  - `async def orphan_ids(session, project: Project) -> list[str]`: ids in `source_index_state` that start with
    `f"{project.id}/"` but are not in `project.files`, sorted.

- [ ] **Step 1: Write the failing tests**. Insert `LogEvent` / `SourceIndexState` rows the same way
  `tests/test_search_coverage.py` does.
  - `test_file_stats_counts_and_parsed_ratio`: 4 events for `p/a`, 3 of them with level and time, gives
    `event_count == 4` and `parsed_ratio == 0.75`.
  - `test_file_stats_leaves_out_ids_without_events`.
  - `test_orphan_ids_are_state_rows_with_no_file`: a state row `p/gone` while the project's files are `p/a` gives
    `["p/gone"]`.
  - `test_orphan_ids_do_not_match_like_wildcards` (Review Focus 1): project `wso2_gw` and a state row `wso2xgw/a` give
    `[]`. Project `a` and a state row `ab/x` give `[]`.

- [ ] **Step 2: Run them and see them fail**: `.venv/Scripts/python -m pytest tests/test_project_coverage.py -q`. Expected: ImportError.

- [ ] **Step 3: Implement.** For `orphan_ids`, use `SourceIndexState.source_id.startswith(prefix, autoescape=True)`.

- [ ] **Step 4: Run the tests and see them pass**: same command. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: per-file event stats and missing-file lookup for projects"`

---

### Task 3: Projects API (read-only) and project-scoped search

**Files:**
- Create: `app/schemas/projects.py`, `app/controllers/projects.py`, `tests/project_helpers.py`, `tests/test_projects_api.py`
- Modify: `app/controllers/dependencies.py`, `app/controllers/router.py`, `app/controllers/health.py` (delete
  `/sources` and `SourceInfo`), delete `app/controllers/tools.py`, `tests/test_api.py` (move its search tests to the new path)

**Interfaces:**
- Consumes: `scan_projects`, `find_project`, `Project` (Task 1); `file_stats`, `orphan_ids` (Task 2); `coverage_for_all`.
- Produces:
  - `dependencies.get_projects() -> tuple[Project, ...]`, which scans `get_settings().logs_root` on every request.
  - `dependencies.get_project(project_id: str, projects = Depends(get_projects)) -> Project`, using `find_project`.
  - `dependencies.get_chat_project(project = Depends(get_project)) -> Project`, which raises
    `ProjectInvalidError` (a new `AppError` in `app/core/errors.py`: status 409, code `"project_invalid"`, message
    `f"Project '{id}' is invalid: {error}"`) when `project.status == "invalid"`.
  - Schemas:
    - `ProjectSummary{id, title, type, status: Literal["ok","invalid"], file_count, indexed_from: datetime|None, indexed_to: datetime|None, error: str|None}`
    - `ProjectDetail(ProjectSummary){description, timezone, include}`
    - `ProjectFile{id, name, size_bytes: int|None, modified_at: datetime|None, status: Literal["not_indexed","error","stale","partial","complete","missing","skipped"], event_count: int, first_at, last_at, parsed_ratio: float|None, last_error: str|None}`.
    In `ProjectFile`, `id` is the short id (the stem). `name` is the file name, or the short id for a missing file.
  - Routes in `app/controllers/projects.py` (prefix `/projects`, tag `Projects`):
    - `GET ""` returns `list[ProjectSummary]`.
    - `GET "/{project_id}"` returns `ProjectDetail`.
    - `GET "/{project_id}/files"` returns `list[ProjectFile]`: files first (in scan order), then skipped, then missing.
    - `POST "/{project_id}/search-logs"` returns `SearchLogsResponse`, calling the existing `search_logs` with
      `sources=project.files`.
  - `tests/project_helpers.py`: `make_project(id="demo", *, files=(), type="wso2", timezone="UTC", title=None, description="", error=None, skipped=()) -> Project`.

- [ ] **Step 1: Write the failing tests** in `tests/test_projects_api.py` (integration). Use a `tmp_path` logs root and
  override `get_projects` through `app.dependency_overrides`, clearing the overrides after each test.
  - `test_list_shows_ok_and_invalid_projects`: one valid and one broken project, with the broken one's `error` set.
  - `test_unknown_and_malformed_ids_share_one_404` with `/projects/UNKNOWN`, `/projects/no-such-project` and
    `/projects/%2E%2E%2Fetc`: each is 404 with `error.message == "No such project."`.
  - `test_files_lists_indexed_skipped_and_missing`: after indexing one file with `index_source`, plus a state row for
    a deleted file and one badly named file, the statuses are `["complete","skipped","missing"]`, and the indexed
    file's `event_count` > 0 and `parsed_ratio` is not None.
  - `test_search_logs_is_limited_to_the_project`: index the same fixture file (same name) into projects `a` and `b`.
    Both `GET /projects/a/files` and `GET /projects/b/files` report the same non-zero `event_count`: neither project
    overwrote the other (spec §9.4). A search in `a` returns only `a/…` source ids. `source_ids=["b/x"]` returns 404.
  - `test_old_routes_are_gone`: `GET /sources`, `POST /tools/search-logs` return 404.

- [ ] **Step 2: Run them and see them fail**: `.venv/Scripts/python -m pytest tests/test_projects_api.py -q`. Expected: FAIL / ImportError.

- [ ] **Step 3: Implement** the schemas, dependencies, controller and router changes. `indexed_from`/`indexed_to` come
  from `time_spans` over the project's files (the min of the starts and the max of the ends).

- [ ] **Step 4: Run the tests**: `.venv/Scripts/python -m pytest tests/test_projects_api.py tests/test_api.py -q`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: read-only projects API and project-scoped search endpoint"`

---

### Task 4: Agent tools scoped to one project

**Files:**
- Modify: `app/agent/tools.py`, `app/tools/search_queries.py` (rename `level_counts` to `group_counts`), `app/agent/graph.py` (`_coverage_note` only)
- Test: `tests/test_agent_tools.py`, `tests/test_summarize_logs_tool.py`, `tests/test_agent_graph.py` (the coverage note tests)

**Interfaces:**
- Consumes: `Project` (Task 1), `make_project` (Task 3).
- Produces:
  - `make_search_logs_tool(*, project_id: str, session_factory=SessionLocal, load_sources: Callable[[], tuple[Source, ...]], embed=embed_query) -> StructuredTool`
  - `make_summarize_logs_tool(*, project_id: str, session_factory=SessionLocal, load_sources: Callable[[], tuple[Source, ...]]) -> StructuredTool`
    (`load_sources` loses its `get_sources` default, so the tools cannot reach every project.)
  - `AgentFilters` no longer inherits `LogFilters`. Its fields: `files: list[str] | None` (description: `"Only files the user named;
    omit to search the whole project"`), `start_time`, `end_time`, `levels`. The method
    `to_filters(project_id: str) -> LogFilters` maps each `f` in `files` to `f"{project_id}/{f}"`.
    `SummaryArgs(AgentFilters)` adds `group_by: Literal["level","day","file"] = "level"`.
  - `async def group_counts(session, where, limit: int, *, group_by: Literal["level","day","file"], timezone: str) -> list[Row]`.
    Rows have `key` (None for `level`, an ISO date string for `day`, a source id for `file`), `level`, `count`,
    `first_at` and `last_at`. Rows are ordered by `count` descending, then `key` and `level`. `day` =
    `func.date(func.timezone(timezone, LogEvent.occurred_at))`.
  - `project_coverage(coverage: list[SourceCoverage], project_id: str) -> dict`, returning
    `{"files": n, "indexed": k, "from": "YYYY-MM-DD"|None, "to": …}`, plus one key per non-complete status mapping to a
    list of short ids. At most 10 names in total; beyond that, `"more": m`.
  - Tool output uses short ids everywhere: a hit's `"file"` replaces `"source_id"`, and a `file`-grouped summary row
    is `{"file": short, …}`.
  - `graph._coverage_note` reads the new coverage object (status keys mapping to lists of short ids) instead of the old list.

- [ ] **Step 1: Write the failing tests**
  - `test_search_tool_never_sees_other_projects`: `load_sources` returns project `a`'s files. `files=["x"]`, where `x`
    exists only in project `b`, returns `{"error": …}` whose text lists `a`'s short ids and contains no `/`.
  - `test_full_ids_in_files_are_rejected_not_doubled` (Review Focus 4): `files=["a/x"]` gives an error, and the
    message does not contain `a/a/`.
  - `test_hits_use_short_file_ids`: each hit has `"file": "wso2carbon-10-30-2026.1"` and no `source_id` key.
  - `test_coverage_is_one_object_for_the_project`: 12 not-indexed files give `coverage["files"] == 12`, at most 10
    names listed, and `coverage["more"] == 2`.
  - `test_empty_project_coverage` (Review Focus 5): no files gives `{"files": 0, "indexed": 0, "from": None, "to": None}`.
  - `test_summary_group_by_day_uses_project_timezone` (integration): an event at `2026-10-29T20:00:00Z` with project
    timezone `Asia/Kolkata` is counted on `"2026-10-30"`.
  - `test_summary_rows_are_ordered_by_count`: with 31 groups, the cut-off group is the smallest, and the note mentions
    narrowing by time or level.
  - `test_coverage_note_reads_the_project_object` in `test_agent_graph.py`: a tool result with `"stale": ["x"]` gives a
    note containing `x: the index is stale`.
  - Update existing tests that call `make_search_logs_tool()` / `make_summarize_logs_tool()` with no arguments to pass
    `project_id="demo"` and a `load_sources`.

- [ ] **Step 2: Run them and see them fail**: `.venv/Scripts/python -m pytest tests/test_agent_tools.py tests/test_summarize_logs_tool.py tests/test_agent_graph.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - The unknown-file error message is `f"Unknown file '{f}'. Files in this project: {short_ids}"`. Catch it before calling
    `searched_sources`, so the model never sees a full id.
  - Update the summary's note text to `"Only the largest 30 groups are shown; narrow by time or level for the rest."`.

- [ ] **Step 4: Run the tests**: same command plus `tests/test_search_units.py`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: scope agent tools to one project with short file ids and a coverage summary"`

---

### Task 5: Project prompt, agent and chat endpoint

**Files:**
- Modify: `app/agent/prompt.py`, `app/agent/graph.py` (`build_agent`), `app/controllers/dependencies.py` (`get_agent`),
  `app/controllers/projects.py` (add chat), `app/controllers/chat.py` (remove `POST /ai/chat`; keep memories and resume)
- Test: `tests/test_agent_prompt.py`, `tests/test_agent_graph.py`, `tests/test_projects_api.py`

**Interfaces:**
- Consumes: `Project`, `make_project`, tool factories (Task 4), `get_chat_project` (Task 3).
- Produces:
  - `build_system_prompt(project: Project, spans: Spans | None = None, *, now: datetime | None = None) -> str`
  - `TYPE_HINTS: Mapping[str, str]` in `prompt.py`. `wso2` → `"WSO2 Carbon: a line starting `TID: [` starts an event; user and
    API ids sit inside the message text."`; the other types → `""`.
  - `build_agent(*, project: Project, model=None, tool=None, summary_tool=None, spans=None) -> Agent`. It builds both tools with
    `project_id=project.id` and `load_sources=lambda: project.files`. The `sources` parameter is removed.
  - `dependencies.get_agent(project = Depends(get_chat_project), session = Depends(get_session)) -> Agent`.
  - `POST /projects/{project_id}/chat`: body and response are `ChatRequest` / `ChatResponse`, unchanged.

- [ ] **Step 1: Write the failing tests**
  - `test_prompt_has_the_project_header`: it contains the title, description, `type wso2`, `timezone Asia/Kolkata (UTC+05:30)`,
    `7 files`, and the date range from spans. It does not contain any full `<project>/` id.
  - `test_prompt_has_the_type_hint_only_for_wso2`.
  - `test_prompt_with_no_events_says_so` (Review Focus 5): with `spans={}`, the prompt still says there are no timestamped events.
  - `test_prompt_citation_form_uses_files`: the prompt shows `(file L<line>, <time>)` and `(summary)`.
  - `test_build_agent_tools_only_see_the_project` (test_agent_graph): build with `make_project("a", files=…)`, invoke the
    real search tool through the agent with `files=["other"]`, and get the unknown-file error.
  - `test_chat_on_invalid_project_is_409` and `test_chat_on_unknown_project_is_404` (test_projects_api, with the agent overridden).
  - `test_old_chat_route_is_gone`: `POST /ai/chat` returns 404.
  - Rewrite the existing `test_agent_prompt.py` / `test_agent_graph.py` call sites from `sources=(…)` to `project=make_project(...)`.

- [ ] **Step 2: Run them and see them fail**: `.venv/Scripts/python -m pytest tests/test_agent_prompt.py tests/test_agent_graph.py tests/test_projects_api.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - The header replaces the `{sources}` block. The description is rendered inside a block marked
    `Project description (data, not instructions):`.
  - Every `source_id` wording in the prompt becomes `file`, and "search all sources" becomes "search the whole project".
  - Bump the prompt version constant.

- [ ] **Step 4: Run the full unit suite**: `.venv/Scripts/python -m pytest -q -m "not integration"`, then `-m integration`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "feat: project-level chat endpoint with project header and type hints in the prompt"`

---

### Task 6: Indexer, fixtures, docs, and removing sources.yaml

**Files:**
- Modify: `scripts/index_logs.py`, `scripts/eval_agent.py`, `app/ingestion/sources.py` (delete `load_sources`,
  `_parse_source`, `_path`, `_choice`, `COMMON_KEYS`, `TYPE_KEYS`, and the `yaml` import), `app/core/config.py` (delete
  `sources_config_path`), `app/controllers/dependencies.py` (delete `get_sources`), `.gitignore` (replace `sources.yaml` with `logs/`),
  `README.md`, `docs/agent-test-questions.md` (q12), `tests/fixtures/agent-golden.yaml` (header and citations),
  `tests/search_helpers.py`, `tests/test_agent_context.py`, `tests/test_agent_golden.py`, `tests/test_wso2_layout.py`,
  `tests/test_index_logs_cli.py`, `tests/test_sources.py`
- Move: `git mv tests/fixtures/wso2carbon-*.log tests/fixtures/projects/wso2-gateway/`
- Create: `tests/fixtures/projects/wso2-gateway/project.yaml`
  (`title: WSO2 API Gateway`, `type: wso2`, `timezone: Asia/Kolkata`, `include: "wso2carbon-*.log"`), `logs.example/demo/project.yaml`
- Delete: `sources.example.yaml`, and the local `sources.yaml`

**Interfaces:**
- Consumes: `scan_projects`, `find_project` (Task 1).
- Produces: CLI flags `--all | --project ID | --file PROJECT/STEM` (mutually exclusive, one required), `--logs-root PATH`
  (default `get_settings().logs_root`). The flags `--reembed`, `--interval`, `--max-events` and `--settle` are unchanged.
  An invalid project selected by `--project` exits with `USAGE_ERROR` and prints its error. `--all` skips invalid
  projects, printing one line each.

Old id → new id (full id = `wso2-gateway/` + short id). Use this for every rename in the golden set and tests:

| Old | New short id |
|---|---|
| `wso2-carbon-0107` | `wso2carbon-01-07-2026.1` |
| `wso2-carbon-0114` | `wso2carbon-01-14-2026.1` |
| `wso2-carbon-0127` | `wso2carbon-01-27-2026` |
| `wso2-carbon-0302` | `wso2carbon-03-02-2026.1` |
| `wso2-carbon-1028` | `wso2carbon-10-28-2026.1` |
| `wso2-carbon-1030` | `wso2carbon-10-30-2026.1` |
| `wso2-carbon-1101` | `wso2carbon-11-01-2026.1` |

- [ ] **Step 1: Rewrite `tests/test_index_logs_cli.py`** for the new flags:
  - `test_project_flag_indexes_only_that_project`
  - `test_file_flag_indexes_one_file`
  - `test_unknown_project_is_a_usage_error`
  - `test_all_skips_invalid_projects_and_says_so`

  Then delete the loader tests from `tests/test_sources.py`, keeping its `get_source` and validator tests.

- [ ] **Step 2: Run them and see them fail**: `.venv/Scripts/python -m pytest tests/test_index_logs_cli.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** the CLI and `eval_agent.py` changes, the fixture move, the renames from the table, the deletions, and the docs.
  - README: setup copies `logs.example/` to `logs/`, plus the 404 rule.
  - q12: "copy a file into a project folder; do not index it".

- [ ] **Step 4: Check that nothing still refers to the old setup.**

  Run: `git grep -nE "sources\.yaml|SOURCES_CONFIG|wso2-carbon-0|load_sources|get_sources|/ai/chat|/tools/search-logs"`
  Expected: matches only in `docs/superpowers/specs/`, `docs/superpowers/plans/` and `docs/*.json` eval records.

  Then run the full suite: `.venv/Scripts/python -m pytest -q`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "refactor: index project folders and remove sources.yaml"`

---

### Task 7: Project picker in the chat UI

**Files:**
- Modify: `frontend/src/api.ts`, `frontend/src/App.tsx`

**Interfaces:**
- Consumes: `GET /projects`, `POST /projects/{id}/chat` (Tasks 3 and 5).
- Produces:
  - `request` accepts `'GET'` too.
  - `interface ProjectSummary { id: string; title: string; type: string; status: 'ok' | 'invalid'; file_count: number; indexed_from: string | null; indexed_to: string | null; error: string | null }`
  - `listProjects(): Promise<ProjectSummary[]>`
  - `sendChat(projectId: string, message: string, conversationId: string | null)`, which calls
    `POST /projects/${encodeURIComponent(projectId)}/chat`.

- [ ] **Step 1: Implement in `App.tsx`.**
  - A Radix `Select` in the header, loaded once with `listProjects()`. Invalid projects are `disabled`, with
    `title={error}` for hover.
  - If exactly one project is `ok`, select it.
  - The composer is disabled with the placeholder "Choose a project" until one is selected.
  - Changing project runs the same reset as the existing new-chat action (`setConversationId(null)` and clearing the turns).
  - A failed `listProjects` shows the existing error style.

- [ ] **Step 2: Type-check and lint**: `npm --prefix frontend run build` and `npm --prefix frontend run lint`. Expected: exit 0.

- [ ] **Step 3: Verify in the browser.** Start the `api` and `chat-ui` servers from `.claude/launch.json` with
  `LOGS_ROOT=tests/fixtures/projects` plus one broken project folder in a temp root. Check that:
  - the picker lists both projects, and the broken one can't be selected;
  - a question goes to `/api/v1/projects/wso2-gateway/chat` (network tab) and an answer comes back;
  - switching project clears the conversation.

  Take a screenshot.

- [ ] **Step 4: Commit**: `git commit -m "feat: project picker in the chat UI"`

---

### Task 8: Re-index and re-run the evals

Needs the user's go-ahead before Step 1: it empties the app database's log tables (test data only, §7).

- [ ] **Step 1: Empty the old ids.** Run `TRUNCATE log_events, source_index_state` on the **app** database (not `_test`).
- [ ] **Step 2: Re-index.** Run `LOGS_ROOT=tests/fixtures/projects .venv/Scripts/python -m scripts.index_logs --all --reembed`.
  Expected: 7 lines, each `complete`.
- [ ] **Step 3: Golden set.** Run the agent golden set and the token golden set (`scripts/eval_agent.py`, as before).
  Expected: scores >= the last recorded run in `docs/agent-eval-*.json`, and tokens per question no higher than
  `tests/fixtures/agent-tokens-golden.yaml`.
- [ ] **Step 4: `files` kept vs removed (§6.5).** Re-run the golden set with `files` removed from `AgentFilters`.
  Keep the variant with the equal-or-better score and fewer tokens. If `files` goes, delete it and its tests in one
  commit.
- [ ] **Step 5: Commit the eval record**: `git commit -m "test: re-run agent evals on project-scoped tools"`
