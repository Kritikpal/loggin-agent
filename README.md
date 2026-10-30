# Local AI Log Monitor

Local-first, read-only AI assistant that helps developers investigate application failures by searching local log
files and citing the exact evidence. Only local log files are supported (Docker support was dropped). Plan: Notion
"Local AI Log Monitor - MVP Plan".

**Current state:** indexing and `search_logs` are real; the AI chat endpoints are mocks.

| Endpoint (under `/api/v1`) | Behaviour |
| --- | --- |
| `GET /health` | `{"status": "ok"}` |
| `GET /sources` | configured sources with their index coverage |
| `POST /tools/search-logs` | hybrid log search (see below) |
| `POST /ai/chat` | mock reply |
| `GET /ai/memories` | mock list |
| `POST /ai/workflows/{thread_id}/resume` | mock reply |

Layout: `app/controllers` (routes), `app/tools` (search tool), `app/schemas`, `app/core` (settings, database,
errors, `llm.py` = LangChain/Groq model with API-key rotation), `app/ingestion` (indexer). Migrations in `alembic/`.

Run: `.venv/Scripts/python -m uvicorn app.main:app --reload`; test: `.venv/Scripts/python -m pytest`.

## Indexing logs

1. Copy `sources.example.yaml` to `sources.yaml` and list the log files the indexer may read (nothing else is ever
   read; `type: file` is the only source type).
2. Index:

   ```
   .venv/Scripts/python -m scripts.index_logs --source demo-app      # one source
   .venv/Scripts/python -m scripts.index_logs --all --reembed        # all, then embed rows that have no vector yet
   .venv/Scripts/python -m scripts.index_logs --all --interval 60    # poll every 60 s until Ctrl-C
   ```

   Options: `--max-events N` (per batch, default 1000), `--settle SECONDS` (how long a file must be quiet before its
   newest event counts as finished, default 5), `--config PATH`. Exit code 1 if a source ended in error.

Each run resumes where the last one stopped (the cursor in `source_index_state`), so re-running stores nothing twice;
a rotated or replaced file is read again from its start and old rows are kept as history. Per event, `log_events`
holds the redacted event text, its time, level and parsed fields, a locator (path, byte offsets, line numbers, sha256)
pointing at the original, the text used for search, and a local 384-dim embedding (fastembed `bge-small`, cached in
`.cache/fastembed`, override with `EMBEDDING_CACHE_DIR`). If embedding fails the event is still stored; `--reembed`
fills it later.

**Safety:** log files are only ever opened for reading. Secrets and personal data (passwords, tokens, auth headers,
keys, emails, card numbers) are redacted before anything is parsed, stored or embedded, and no LLM is involved in
indexing. Errors are recorded by exception class name only, never with log content.

## Searching logs

`POST /api/v1/tools/search-logs` (read-only; no LLM):

```json
{"query": "why did startup fail", "source_ids": ["demo-app"], "levels": ["ERROR", "WARN"],
 "start_time": "2026-03-10T00:00:00Z", "end_time": null, "top_k": 10}
```

Only `query` is required (1-2000 chars); `top_k` is 1-50 (default 10); `source_ids` omitted = every configured
source; an id not in `sources.yaml` is a `404`, a malformed body a `422`. The response:

```json
{"results": [{"event_id": "...", "source_id": "demo-app", "occurred_at": "...", "level": "ERROR",
              "locator": {"type": "file", "path": "...", "line_start": 158, "line_end": 158,
                          "offset_start": 0, "offset_end": 0, "sha256": "..."},
              "raw_text": "...", "score": 0.032, "matched_by": ["semantic", "keyword"]}],
 "coverage": [{"source_id": "demo-app", "status": "complete", "indexed_through": "...", "last_error": null}]}
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

`GET /api/v1/sources` lists every configured source as `{"id", "type", "format", "status", "indexed_through",
"last_error"}` with the same coverage status (`[]` when none are configured).
