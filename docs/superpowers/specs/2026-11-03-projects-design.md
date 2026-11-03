# Projects: managing log files per application - design

Date: 2026-11-03
Status: draft, awaiting review.
Replaces: `sources.yaml` as the allowlist of log files.
Touches: `app/ingestion/{sources,coverage}.py`, `app/controllers/{dependencies,chat,health,tools}.py`,
`app/agent/{graph,tools,prompt}.py`, `scripts/{index_logs,eval_agent}.py`, `frontend/src/api.ts` and the chat page,
`README.md`, `docs/agent-test-questions.md`.

## 1. Problem

Today every log file is a separate "source" in `sources.yaml`:

- **Repeated settings.** The 7 WSO2 files each repeat `event_start`, `timezone` and `app_packages`. Those belong to the
  application that wrote the logs, not to each file.
- **A config edit for every file.** WSO2 rotates its log daily, so every new day means a new YAML entry.
- **Chat searches everything.** The agent searches all sources unless the user names one. Once a second application's
  logs are added, answers will mix logs from unrelated systems.
- **Coverage output grows with the number of files.** Every tool result lists coverage for each file. With 30 daily files that is
  30 rows in every search result, under Groq's limit of 8,000 tokens a minute (ADR 0003).

## 2. Goals and non-goals

**Goals**

- A **Project** is the main entity: title, description, type, and any number of log files.
- Adding a log file to a project means copying it onto the server. No config edit, no database write.
- A chat belongs to exactly one project and can only see that project's files.
- Read-only API to view projects and their files, including whether each file is indexed and parsed well.

**Non-goals (for now)**

- Creating, editing or deleting projects through the API; uploading files.
- Chats that cover several projects. A separate endpoint can add this later without changing the one here.
- Environments (prod / staging) inside a project.
- Saved conversation history (`conversation_id` is still only echoed back).
- Retention or automatic deletion of old events.

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | A project is a **folder** under one logs root. Its metadata is in `project.yaml` inside the folder. | Files are copied onto the server, so copying a file into the folder is the whole workflow. |
| D2 | Project metadata is not stored in the database. The folders are the only source of truth. | Nothing to keep in sync. The logs root replaces the `sources.yaml` allowlist. |
| D3 | `type` is a **named preset** that fills in the parsing settings. `project.yaml` can override them. | Removes the repeated settings, and matches what users mean by "type". |
| D4 | File id = `<project_id>/<file stem>`, e.g. `wso2-gateway/wso2carbon-10-30-2026.1`. | `log_events` is unique on `(source_id, event_key)`, so two projects with the same file name would overwrite each other's events. |
| D5 | Chat is `POST /projects/{project_id}/chat`, one project per chat. | One project per chat, chosen in the brainstorm. |
| D6 | The agent's tools are **built with only the project's files**. The model cannot reach other projects. | Scoping is enforced in code, not left to the prompt. |
| D7 | No change to search ranking, filters or the hybrid query. | Not needed for this change. |

## 4. Folder layout

```
<LOGS_ROOT>/
  wso2-gateway/
    project.yaml
    wso2carbon-10-28-2026.1.log
    wso2carbon-10-30-2026.1.log
  payments-api/
    project.yaml
    app-2026-11-01.log
```

- `LOGS_ROOT` is a setting (env `LOGS_ROOT`) and replaces `SOURCES_CONFIG_PATH`.
- Each direct subfolder containing a `project.yaml` is a project. A folder without one is skipped and a warning is logged.
- The folder name is the `project_id`. It must match `[a-z0-9][a-z0-9_.-]{0,63}`, the same pattern as today's source ids.
- **Project files** are the regular files directly in the folder, excluding `project.yaml` and hidden files, and matching
  `include` (default `*.log*`). Subfolders are not read; they are reserved for environments later.
- Symlinks are resolved, and a file whose real path is outside the project folder is skipped with a warning.

### `project.yaml`

```yaml
title: WSO2 API Gateway                 # required, 1-120 chars
description: Production gateway, Mumbai cluster. Fronts the payments and auth APIs.   # optional, <= 300 chars (sent with every model call)
type: wso2                              # required, one of the presets below
timezone: Asia/Kolkata                  # optional, default UTC
include: "wso2carbon-*.log"             # optional glob, default "*.log*"
# Optional overrides of the preset:
# event_start: '^TID: \['
# app_packages: [org.wso2]
# field_mappings: {...}
```

Validation is as strict as today's `_parse_source`: unknown keys, a bad regex, an unknown timezone or a bad package name
are a `ConfigError` for that project. **One bad project does not hide the others.** It is reported in `GET /projects`
with `status: "invalid"` and the error message, and it cannot be chatted with.

### Type presets

| `type` | `format` | `event_start` | `app_packages` | Notes |
|---|---|---|---|---|
| `wso2` | text | `^TID: \[` | `[org.wso2]` | WSO2 Carbon log |
| `spring-boot` | text | default preset | `[]` | Matched by the `SPRING_BOOT` layout |
| `logback` | text | default preset | `[]` | Matched by the `LOGBACK_DEFAULT` layout |
| `json` | json | n/a | `[]` | One JSON object per line |

Presets live in code next to the layouts in `app/ingestion/fields/text_layouts.py`. A new type means a new layout plus a
new preset row.

### File ids

- `<project_id>/<stem>`, where `stem` is the file name without its last extension, lower-cased. The stem must match
  the same pattern as a project id (`[a-z0-9][a-z0-9_.-]{0,63}`), so a full id is at most 129 characters. The
  `SOURCE_ID` check in `sources.py` becomes "project pattern `/` stem pattern".
- A file whose stem does not match is **skipped, not fatal**: it is not indexed, and it is listed in
  `GET /projects/{id}/files` with `status: "skipped"` and the reason, so the operator sees it. One badly named file
  must not take the whole project offline.
- If two files in one project produce the same id, both are skipped the same way. Silently overwriting events is
  worse.
- **Renaming a file changes its id.** The renamed file is indexed again under the new id, and the old events stay under the old one.
  Rule for operators: never rename a file after copying it in, and copy only rotated files, never the live
  `wso2carbon.log`. Identifying files by a content hash is the fix if this becomes a problem.

## 5. API

| Method and path | Returns |
|---|---|
| `GET /projects` | `[{id, title, type, status, file_count, indexed_from, indexed_to, error?}]`, where `status` is `ok` or `invalid` |
| `GET /projects/{project_id}` | The above plus `description`, `timezone`, `include` |
| `GET /projects/{project_id}/files` | `[{id, name, size_bytes, modified_at, status, event_count, first_at, last_at, parsed_ratio, last_error?}]` |
| `POST /projects/{project_id}/chat` | Same body and response as today's `POST /ai/chat` |
| `POST /projects/{project_id}/search-logs` | Same as today's `POST /tools/search-logs`, limited to the project |

- File `status` reuses the statuses `app/ingestion/coverage.py` already computes: `not_indexed`, `error`, `stale` (the file changed
  after the last run), `partial` (its tail is not consumed yet) and `complete`. Two are new:
  - `missing`: events exist, but the file is gone. Today a vanished file counts as `stale`. A file can only be
    `missing` if something still lists it, so the files endpoint also lists ids that have `source_index_state` rows
    with no file. Missing files are shown, not hidden. Deleting their events is a separate, explicit action (not in
    this change).
  - `skipped`: the file name cannot become an id (§4, file ids).
- `parsed_ratio`: the share of the file's events that got a level and a time from the type's layout. A low ratio is how an
  operator spots a file copied into the wrong project or a wrong timezone. It is computed from `log_events` at query time.
- Removed: `POST /ai/chat`, `POST /tools/search-logs`, `GET /sources`. `GET /ai/memories` and the workflow resume route
  stay as they are.
- **Frontend (phase 1, same change, no shim):** `frontend/src/api.ts` calls `POST /ai/chat` today. The chat page gets a
  project picker filled from `GET /projects`. Projects with `status: "invalid"` are shown but cannot be selected, and
  their error appears on hover. The picker's choice goes into the path: `POST /projects/{id}/chat`. Changing project
  starts a new conversation. If there is only one valid project, it is selected automatically.
- Tool ids in `ChatResponse.tool_calls` are the short ids the model saw (§6.2). The project is already known from the
  URL.

### Security

- `project_id` from the path is **only looked up** in the projects found under `LOGS_ROOT`, and never joined into a path.
  An unknown or malformed id returns 404 with the same message for both cases.
- No endpoint accepts a file path. Every file read comes from scanning `LOGS_ROOT`.
- `title`, `description` and file names come from someone with server access, so they are trusted as much as today's
  `sources.yaml`. The description is still rendered into the prompt as data, inside a clearly marked block.

## 6. Agent and tools

### 6.1 Scoping (the bug this fixes)

`build_agent(sources=...)` today only uses `sources` for the prompt. The tools are built with their default
`load_sources=get_sources`, which reads **every** source (`app/agent/graph.py:43`). Change:

```python
files = project.files
tools = [make_search_logs_tool(load_sources=lambda: files), make_summarize_logs_tool(load_sources=lambda: files)]
```

A `source_ids` value outside the project gets the existing "unknown source" error, whose list of allowed ids now only
contains this project's files.

### 6.2 Shorter ids for the model

Every id in one chat starts with the same `<project_id>/`, so the model never sees that prefix. Tool output removes it, and
tool input adds it back before validation. Citations become `(wso2carbon-10-30-2026.1 L158, 10:14:08Z)`.

### 6.3 One coverage line per project

The agent's tool output (`_compact`, `summarize_logs`) replaces the per-file coverage list with:

```json
"coverage": {"files": 30, "indexed": 28, "from": "2026-01-07", "to": "2026-11-01", "not_indexed": ["wso2carbon-03-02-2026.1"]}
```

Files are listed only when their status is not `complete` (grouped by status, e.g. `"stale": [...]`), and the list is
capped at 10 names, with a count of the rest. The public search API keeps the full list
for each file.

### 6.4 `summarize_logs` gains `group_by`

`group_by: "level" | "day" | "file"`, default `"level"`. `day` uses the project's timezone. With daily files, grouping by
file plus level hits the 30-group cap (`MAX_GROUPS`), and it does not answer "which day had the most errors?". The
prompt's guidance for `summarize_logs` gets one line about when to use `day`.

`day` hits the same cap: day × level rows reach 30 after 10 days with 3 levels. The cap stays (it protects
the token budget), and the existing "only the largest groups are shown" note changes to tell the model to narrow
by time or level. That also keeps the rows sorted by count, so the worst days survive the cut.

### 6.5 `source_ids` → `files`

Rename the argument to `files` (description: "Only files the user named; omit to search the whole project"). Run the
golden set (`tests/fixtures/agent-golden.yaml`) with the argument kept and with it removed, and keep the version that
does better or equally well on fewer tokens.

### 6.6 System prompt

- Replace the list of allowed sources with a project header: title, description, type, timezone, file count, date
  range.
- Add a short **hint block for each type** (e.g. WSO2: "`TID:` starts an event; the user id sits in the message").
  WSO2-specific wording moves out of the general prompt into this block. Bump the prompt version and re-run the
  token golden set (`tests/fixtures/agent-tokens-golden.yaml`).

### 6.7 Phase 2: `read_around`

`read_around(file, line, before=20, after=20)` returns the original lines around a hit, read from the file recorded in
the event's locator. It is limited to the project's files by the same `load_sources` scoping, and it has its own budget.
It is not part of phase 1.

## 7. Indexing and migration

- `scripts/index_logs.py` scans `LOGS_ROOT` instead of reading `sources.yaml`. `--source` becomes `--project` (one
  project) or `--file <project>/<stem>` (one file). `--all` stays. `--config` becomes `--logs-root`.
- Docs and scripts that mention `sources.yaml` are updated: `README.md` (setup and the 404 rule),
  `docs/agent-test-questions.md` (q12), `scripts/eval_agent.py`, and the fixture header in
  `tests/fixtures/agent-golden.yaml`.
- `source_index_state` and `log_events` keep their columns. Only the id values change (D4). No schema migration.
- **Migration:** the existing events use the old ids (`wso2-carbon-1030`). They are test data, so delete them and index again
  rather than renaming them in place.
- Test data: move the 7 WSO2 fixtures to `tests/fixtures/projects/wso2-gateway/` with a `project.yaml`, and point
  tests at `LOGS_ROOT=tests/fixtures/projects`. Update the golden sets' expected citations to the new ids.
- `sources.yaml` and `sources.example.yaml` are deleted. `logs/` is added to `.gitignore`, and a
  `logs.example/demo/project.yaml` documents the format.

## 8. Phases

| Phase | Contents |
|---|---|
| 1 | Folder scan and `project.yaml` (§4), presets, new ids, projects API (§5), chat and search under `/projects/{id}`, frontend project picker (§5), tool scoping (§6.1-6.6), migration (§7) |
| 2 | `read_around`; a project overview with errors per day (built on `group_by: day`) |
| 3 | Memory scoped to a project, when the memory feature is built (the memory specs have no scope yet) |

## 9. Acceptance criteria

1. Copying a file into a project folder and running the indexer makes it appear in `GET /projects/{id}/files` as
   `indexed`, with no other change.
2. A project folder with a broken `project.yaml` shows as `invalid` with its error. The other projects still work.
3. Chat for project A never returns an event or citation from project B, even when the model passes B's file id.
4. Two projects with the same file name index without overwriting each other.
5. `GET /projects/%2E%2E%2Fetc`, `GET /projects/UNKNOWN` and `GET /projects/no-such-project` all return the same 404.
   (A literal `../` is normalised away by HTTP clients before it reaches the app, so the test must send it encoded.)
6. A file with an invalid name is listed as `skipped`, and the rest of its project still indexes and chats.
7. The frontend chat page lists projects, can't select an invalid one, and sends questions to
   `POST /projects/{id}/chat`. Changing project clears the conversation.
8. A search result in the agent carries one coverage object for the project, not one entry per file.
9. `summarize_logs(group_by="day")` over the WSO2 project returns one row per day and level, in Asia/Kolkata days.
10. The golden question set scores at least as well as before the change, and the token golden set does not grow.

## 10. Open questions

1. **`include` default.** Is `*.log*` right, or should it be every file in the folder? `*.log*` also picks up `.log.gz`, which
   the indexer cannot read yet. Either exclude `*.gz` or add gzip support.
2. **When to scan.** Scanning `LOGS_ROOT` on every request is fine for a handful of projects. Cache it if the scan
   shows up in latency.
3. **Deleting a project folder.** Its events stay. Should `GET /projects` list it as `missing`, as files are, or drop it?
