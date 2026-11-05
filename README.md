# Local AI Log Monitor

Local-first, read-only AI assistant that helps developers investigate application failures by searching local log
files and citing the exact evidence. Only local log files are supported (Docker support was dropped). Plan: Notion
"Local AI Log Monitor - MVP Plan".

**Current state:** indexing, search and the project chat agent are real; memories and workflows are mocks.

| Endpoint (under `/api/v1`) | Behaviour |
| --- | --- |
| `GET /health` | `{"status": "ok"}` |
| `GET /projects` | every project folder: title, type, status (`ok`/`invalid`), file count, indexed range |
| `GET /projects/{project_id}` | one project, plus description, timezone and file pattern |
| `GET /projects/{project_id}/files` | its files: status, size, event count, first/last time, share of lines parsed |
| `POST /projects/{project_id}/search-logs` | hybrid log search within one project (see below) |
| `POST /projects/{project_id}/chat` | ask the investigation agent about one project |
| `GET /ai/memories` | mock list |
| `POST /ai/workflows/{thread_id}/resume` | mock reply |

Layout: `app/controllers` (routes), `app/tools` (search tool), `app/schemas`, `app/core` (settings, database,
errors, `llm.py` = LangChain/Groq model with API-key rotation), `app/ingestion` (indexer). Migrations in `alembic/`.

Run: `.venv/Scripts/python -m uvicorn app.main:app --reload`; test: `.venv/Scripts/python -m pytest`.

## Indexing logs

1. A **project** is a folder under `logs/` (override with `LOGS_ROOT`) holding a `project.yaml` and its log files.
   Copy `logs.example/` to `logs/` to start; `logs/` is git-ignored. To add a log, copy the file into its project's
   folder. Only files found this way are ever read.

   ```yaml
   title: WSO2 API Gateway                 # required, up to 120 chars
   description: Production gateway         # optional, up to 300 chars; the agent sees it as data
   type: wso2                              # wso2 | spring-boot | logback | json: sets the parsing defaults
   timezone: Asia/Kolkata                  # for timestamps without a zone (default UTC)
   include: "wso2carbon-*.log"             # which files count (default *.log*); subfolders are never read
   # overrides of the type's defaults: event_start, app_packages, field_mappings
   ```

   A file's id is `<project>/<lower-cased file name minus .log, .txt or .json>` (`app.log` -> `app`, `app.log.1` stays `app.log.1`). A file whose name cannot become an
   id (spaces, two files with one id) is skipped and listed as `skipped` with the reason. **Never rename a file after
   copying it in** (it would be indexed again under a new id), and copy only rotated files, never the live log.
   A broken `project.yaml` marks only that project `invalid`.
2. Index:

   ```
   .venv/Scripts/python -m scripts.index_logs --project wso2-gateway                  # one project
   .venv/Scripts/python -m scripts.index_logs --file wso2-gateway/wso2carbon-10-30-2026.1   # one file
   .venv/Scripts/python -m scripts.index_logs --all --reembed      # every valid project, then embed rows with no vector
   .venv/Scripts/python -m scripts.index_logs --all --interval 60  # poll every 60 s until Ctrl-C
   ```

   Options: `--max-events N` (per batch, default 200), `--settle SECONDS` (how long a file must be quiet before its
   newest event counts as finished, default 5), `--logs-root PATH`. Exit code 1 if a file ended in error, 2 for an
   unknown or invalid project.

Each run resumes where the last one stopped (the cursor in `source_index_state`), so re-running stores nothing twice;
a rotated or replaced file is read again from its start and old rows are kept as history. Per event, `log_events`
holds the redacted event text, its time, level and parsed fields, a locator (path, byte offsets, line numbers, sha256)
pointing at the original, the text used for search, and a local 384-dim embedding (fastembed `bge-small`, cached in
`.cache/fastembed`, override with `EMBEDDING_CACHE_DIR`). If embedding fails the event is still stored; `--reembed`
fills it later.

**Safety:** log files are only ever opened for reading. Secrets and personal data (passwords, tokens, auth headers,
keys, emails, card numbers) are redacted before anything is parsed, stored or embedded, and no LLM is involved in
indexing. Errors are recorded by exception class name only, never with log content.

## Pushing logs

An application can send its logs here instead of having files copied in. Add one line to the project's `project.yaml`
and set that environment variable (in `.env`) to a long random token:

```yaml
push_token_env: ACME_PUSH_TOKEN      # name of the environment variable that holds this project's bearer token
```

```
curl -X POST http://localhost:8000/api/v1/projects/wso2-gateway/ingest \
  -H "Authorization: Bearer $ACME_PUSH_TOKEN" -H "Content-Type: application/json" \
  -d '[{"message": "TID: [-1234] [] [2026-11-04 10:00:00,123] ERROR {org.acme.Pool} - pool exhausted"},
       {"message": "payment failed", "timestamp": "2026-11-04T10:00:05Z", "level": "error"}]'
```

The body is a JSON list of events (or NDJSON with `Content-Type: application/x-ndjson`); `message` is required,
`timestamp` (ISO 8601; no zone = the project's timezone) and `level` are optional. The reply is
`{"received": 2, "stored": 2, "duplicates": 0}`. Limits: 10 MiB (`PUSH_MAX_BODY_BYTES`) and 1000 events per request (`413` above either; the message says which);
`401` for a missing or wrong token; `404` for an unknown project, a project without `push_token_env`, or an unset
variable; `409` for an invalid project; `422` for a malformed body; `503` when the database is down (retry).

**Duplicates:** an event is identified by its redacted text plus the sender's `timestamp`, so a retried or overlapping
batch stores nothing twice; two truly identical events with the same timestamp collapse into one. Pushed events are
stored at once and searchable by keyword immediately; a loop inside the app adds their embeddings every
`PUSH_EMBED_INTERVAL_SECONDS` (default 5). They appear in the project's files list as `push`. Send the token over
HTTPS (put the app behind TLS); `push` is a reserved file id for projects that enable this.

## Searching logs

`POST /api/v1/projects/{project_id}/search-logs` (read-only; no LLM):

```json
{"query": "why did startup fail", "source_ids": ["demo/app"], "levels": ["ERROR", "WARN"],
 "start_time": "2026-03-10T00:00:00Z", "end_time": null, "top_k": 10}
```

Only `query` is required (1-2000 chars); `top_k` is 1-50 (default 10); `source_ids` omitted = every file of the
project; a file id outside the project is a `404`, as is an unknown project (always `"No such project."`); an
invalid project is a `409`; a malformed body a `422`. The response:

```json
{"results": [{"event_id": "...", "source_id": "demo/app", "occurred_at": "...", "level": "ERROR",
              "locator": {"type": "file", "path": "...", "line_start": 158, "line_end": 158,
                          "offset_start": 0, "offset_end": 0, "sha256": "..."},
              "raw_text": "...", "score": 0.032, "matched_by": ["semantic", "keyword"]}],
 "coverage": [{"source_id": "demo/app", "status": "complete", "indexed_through": "...", "last_error": null}]}
```

`locator` leads back to the exact bytes of the original file (byte offsets, lines, sha256); `raw_text` is the redacted
event text, returned as data only.

**Ranking.** Two legs each fetch their best 50 candidates, with all filters (sources, levels, time window) applied
inside their SQL, so filters work before `top_k`, never after:
- *semantic*: cosine distance between the question's embedding and each event's (HNSW index with pgvector's
  iterative scan, so a filtered search still fills its page; events without a vector are skipped);
- *keyword*: full-text match on the event's search text, OR-style: any lexeme of the question matches, stop words are
  dropped, identifiers are kept whole and split (`jwtAuthFilter` -> `jwtauthfilter`, `jwt`, `auth`, `filter`).

Reciprocal rank fusion merges them: `score = sum over legs of 1 / (60 + rank)`; ties go to the newest event.
`matched_by` names the legs that found a hit. There is no similarity cutoff: a question always gets the nearest
events, so read `score`/`matched_by`, not mere presence. If embedding the question fails, the search runs keyword-only.

**Coverage** is always reported for every searched source, so "no results" is never mistaken for "no logs":
`not_indexed` (never indexed), `error` (the last run recorded an error, see `last_error`), `stale` (the file changed
after the last run: re-index), `partial` (unchanged file whose newest part is not indexed yet, e.g. an event held back
while still being written), `complete`.

`GET /api/v1/projects/{project_id}/files` reports the same status per file, plus `missing` (indexed, but the file
is gone; its events are kept) and `skipped` (the name cannot become an id).
