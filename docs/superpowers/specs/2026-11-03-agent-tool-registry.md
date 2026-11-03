# Agent tools: requests, responses and workflow

Date: 2026-11-03. Examples are real values from the `wso2-gateway` project.

## Tools

| Tool | Job |
|---|---|
| `overview` | How many events, and what kinds? Start here |
| `find` | Every event of a kind or with exact text, with the total. With only a time window: the timeline |
| `search_logs` | Events similar in meaning, when the exact words are unknown |

Retired: `summarize_logs` (replaced by `overview`).

Common to all three:
- Only the chat's project files are visible. Files are named by short id, e.g. `wso2carbon-10-30-2026.1`.
- `sig` is an event kind's id (8 hex). Copy it from a result; never invent one.
- Times in: ISO 8601 with an offset (`2026-10-30T18:35:00+05:30`). Times out: UTC.
- `min_level` means this level and worse (DEBUG < INFO < WARN < ERROR).
- An error comes back as `error: <what to fix>` and does not use up the budget.

## 1. `overview`

**Request**

```python
class OverviewArgs(BaseModel):
    start_time: datetime | None = Field(None, description="ISO 8601 with offset")
    end_time:   datetime | None = Field(None, description="ISO 8601 with offset")
    min_level:  Level = Field("INFO", description="This level and worse; DEBUG only if asked")
    by_day:     bool = Field(False, description="Also count per day")
    top:        int = Field(10, ge=1)      # max 25
```

**Response** (30 October, `min_level: "ERROR"`)

```
window 2026-10-29T18:30:00Z..2026-10-30T18:29:59Z · total 132 · ERROR 132 · hidden none
columns: sig count level first last template
7dcd9c77 30 ERROR L3079@10:49Z L10966@13:18Z {AttributeMapper} CharonException: Error in parsing date time…ISO_OFFSET_DATE_TIME format
6d07260c 26 ERROR L156@08:31Z  L21773@14:30Z {SCIMUserManager} Error while updating attributes of user: <*> UserStoreException…Privilege Block
9efbfe67 17 ERROR L346@08:31Z  L21868@14:30Z {AbstractResourceManager} Error while updating attributes of user: <*> CharonException…
… 15 more kinds
file wso2carbon-10-30-2026.1 · files 7, indexed 7
```

- Kinds are ordered by count. `hidden` names the levels left out (e.g. `hidden DEBUG 2754`).
- `by_day: true` adds a count per day under each kind.

## 2. `find`

**Request**

```python
class FindArgs(BaseModel):
    sig:  str | None = Field(None, description="A sig copied from an earlier result")
    text: str | None = Field(None, max_length=200, description="Exact words, any case")
    start_time: datetime | None = Field(None, description="ISO 8601 with offset")
    end_time:   datetime | None = Field(None, description="ISO 8601 with offset")
    min_level:  Level | None = Field(None, description="Default: none for sig/text, INFO for a window")
    limit: int = Field(10, ge=1)      # max 25
    # at most one of sig / text; with neither, both times are required and at most 2 hours apart
```

**Modes**

| Mode | Request | Result |
|---|---|---|
| By kind | `find(sig="1f73cf09")` | L67 and L68 (2026-03-02 10:38:54Z, 10:40:50Z) |
| By text | `find(text="Starting WSO2 Carbon")` | `total 7`: L2, L4844, L9609, L10997, L15511, L18927, L22520 |
| Timeline | `find(start_time="2026-10-30T18:35+05:30", end_time="2026-10-30T18:50+05:30")` | Shutdown L9579, start L9609, started L9690 |

**Response** (by text)

```
file wso2carbon-10-30-2026.1 · day 2026-10-30 · total 7 · shown 7 · columns: time line level sig text
07:53:17Z L2     INFO 2452d45d Starting WSO2 Carbon...
11:48:17Z L4844  INFO 2452d45d Starting WSO2 Carbon...
13:11:29Z L9609  INFO 2452d45d Starting WSO2 Carbon...
…
files 7, indexed 7
```

**Response** (timeline: repeats folded)

```
window 13:05:00Z..13:20:00Z · events 2861 · hidden DEBUG 2754 · columns: time line level sig text [×repeat until]
13:11:00Z L9579  INFO  aeede701 Shutdown hook triggered....
13:11:05Z L9606  INFO  f35e41be Shutdown complete
13:11:29Z L9609  INFO  2452d45d Starting WSO2 Carbon...
13:11:45Z L9618  INFO  c840d966 At least one JAR was scanned for TLDs… ×11 until 13:14:54Z
13:14:53Z L9690  INFO  d33196b6 WSO2 Carbon started in 218 sec
13:18:56Z L10945 ERROR 7dcd9c77 CharonException: Error in parsing date time… ×22
files 7, indexed 7
```

- `text` also matches inside stack traces (`error code 49` finds 5 events).
- `total` counts every match. When more exist than are shown: `note: showing 10 of 132; narrow by time, level or sig`.
- Rows show `MM-DD` when results span several days. A result is capped at about 4,000 characters.

## 3. `search_logs`

**Request** (unchanged)

```python
class SearchArgs(BaseModel):
    query: str = Field(max_length=2000, description="Search text")
    top_k: int = Field(5, ge=1)          # 5 shown, 10 with levels
    start_time: datetime | None = Field(None, description="ISO 8601 with offset")
    end_time:   datetime | None = Field(None, description="ISO 8601 with offset")
    levels: list[str] | None = Field(None, description="e.g. ERROR, WARN")
```

**Response**: same compact rows as `find`, now with `sig`, so a good hit can be followed by `find(sig=…)`. Hits are a
sample: never count them.

## Workflow

```
question
  │
  ├─ refusal / off-topic ──────────────────────────────► answer (no tools)
  │
  ├─ "how many / what kinds" ──► overview ─────────────► answer
  │                                 └─ need the events ─► find(sig) ──► answer
  │
  ├─ exact text or identifier ─► find(text) ───────────► answer
  │
  ├─ "what happened at T" ─────► find(window) ─────────► answer
  │
  └─ unknown wording ──────────► search_logs ──► find(sig) ──► answer
```

| Rule | Value |
|---|---|
| Calls per question | `overview` 1, `find` 2, `search_logs` 2; at most 4 tool calls in total |
| Typical question | 2-3 model calls |
| Answer citations | An event: `(file L<line>, <time>)`. A count: `(overview, <sig>)` |
| Rate limit | 8,000 tokens per minute (setting). A call that would go over waits up to 60 s; past that the answer is "the AI is busy, try again in a minute" |
| Earlier results | Condensed on later calls; the latest results stay whole |
