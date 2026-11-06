# `count_by`: an agent tool that counts by field (step 2 of countable log analytics)

Date: 2026-11-06 · Status: draft for review · Branch: feat/ai-action-approval-workflow
Builds on: `2026-11-05-audit-field-extraction-design.md` (step 1: the `audit_*` fields).

## Goal

Questions such as "how many logins on 4 November", "which user / application logged in most", "which application has the
most failures", "login report per day" and "how many different users" are answered by an exact count computed in SQL,
never by the model counting a sample of 25 lines.

## Evidence

The audit golden set (`tests/fixtures/agent-golden-audit.yaml`, 10 questions) was run on 2026-11-06 with the three
existing tools. Baseline: 5 of 10 truly correct (the scorer said 6; a4 was a false pass). The correct ones were single
event kinds that happen to equal the thing counted (a1, a3, a9, a10) plus the hidden-address case (a8). Every question that
crosses kinds failed: a2 (said 3, truth 8), a4 (Console 5, truth My Account 10), a5 ("no failed logins", truth 4), a6 (wrong
table), a7 (one of four users). No tool reads the `audit_*` fields, so the model could not have used them
(`grep audit_ app/agent app/search` is empty).

## Design

### 1. One generic tool, with a fixed vocabulary

`count_by`, registered like the others in `app/agent/tools/__init__.py` (`ToolSpec("count_by", make_count_by_tool,
BUDGET, digest_count)`), built in a new file `app/agent/tools/count_by.py`. One tool for all five scenarios, not one tool per
question, because they differ only in what is grouped and filtered.

Arguments (a pydantic `ToolArgs` model, like `FindArgs`):

| Argument | Type | Meaning |
|---|---|---|
| `by` | list of 1-2 of `action`, `result`, `user`, `actor`, `application`, `target`, `level`, `day`, `hour` | what to group by; two values give a cross table (e.g. `[day, result]`). An enum, so the model cannot invent a field. |
| `action`, `result`, `application`, `user` | optional strings | keep only events with this value (case-insensitive, exact) |
| `start_time`, `end_time` | optional datetimes (same help text as the other tools) | the window; `day` and `hour` are in the project's timezone |
| `top` | int, default 15, max 50 | groups shown |

Each name in `by` maps to a fixed SQL expression in a dict (`user` -> `fields->>'audit_user'`, `day` ->
`to_char(occurred_at at time zone :tz, 'YYYY-MM-DD')`, `level` -> the `level` column, ...). No model text is ever put in SQL;
every filter value is a bound parameter. The tool only reads the chosen project's sources (`project.sources`), exactly as
`overview` does, and uses `SessionLocal`, read-only SELECTs.

`user` means `audit_user` where the event has one, else `audit_actor`; `actor` and `target` are the raw fields. `level`,
`day` and `hour` work for any project; the `audit_*` dimensions exist for WSO2 audit events only (spec step 1).

### 2. Output

One header line, then one line per group, largest first (ties by value), through the existing `fitted(...)` size cut and
`coverage_line(...)` footer, so it behaves like `overview` and `find`:

```
window 2026-11-03T00:00:00Z..2026-11-05T23:59:59Z · filters action=Login result=Failed · by application · total 5 ·
groups 2 · columns: application | count | first | last
portalApplication | 4 | L20@11-03T07:42Z | L72@11-05T06:45Z
Console | 1 | L74@11-05T04:40Z | L74@11-05T04:40Z
```

(Illustrative values; columns are separated by ` | ` because values such as "My Account" contain spaces.) `total` is the
number of events counted in the groups (matching the filters and having every chosen field), `groups` the number of distinct groups, so
"how many different users" is the `groups` figure. `first`/`last` cite the earliest and latest event of the group
(`L<line>@<time>`, as in `overview`) so a count can be cited without another call. When more groups exist than `top`:
"N more groups; raise top (max 50) or narrow the window". Events that lack the chosen field are reported once in the header
("M events have no <field>") and are not counted as a group.

Hidden users: a value of the form `[REDACTED:email:u...]` is shown as is, and the header adds one fixed note when any such
value is shown ("[REDACTED:email:u...] is one person whose address is hidden; the same code is the same person; a plain
[REDACTED:email] is an unknown person"). Addresses are never stored, so the tool cannot reveal one.

Errors: an unknown project or no matching events is a normal result ("total 0", with the coverage footer, so "none" is only
claimed when coverage is complete, as the prompt already requires). A field that no event in scope has gives "no events in
this window have <field>; this project has no audit fields" instead of an empty table. An unexpected failure returns
`error: count_by is temporarily unavailable` and is logged, like `overview`.

### 3. Registry, budget, digest

- `BUDGET = 2` calls per question; the shared cap `MAX_TOOL_CALLS = 4` is unchanged. The scorer's copy of the budgets
  (`scripts/eval_scoring.py` `BUDGETS`) gets `"count_by": 2`.
- `digest_count` in `app/agent/tools/format.py`: when the model sees an earlier result again, keep the header and the first
  10 group lines.
- The test `tests/test_agent_golden.py::test_every_tool_is_required_by_some_case` currently demands that the 17-case set
  requires every registered tool. It will check the union of both golden files instead.

### 4. The model must choose it: description and prompt 4.2

Under prompt 4.0 the use-when rules live in the tool description, and that is where this one goes: "Counts events by one or
two fields: user, application, action, result, day, hour or level. Use for how many logins / failures, who or which
application the most, how many different users, and a report per day. Counts are exact; never count listed events yourself.
2 calls per question." The prompt gets one short rule and a citing form, since counts are cited by `sig` today and this tool
has none: "Cite a `count_by` count as (count_by <by>, <window>)." Prompt version 4.2 with a changelog line, about +40 tokens.
The existing rule "Counts ... come from a tool's totals" already covers the rest.

### 5. Performance

The queries filter on `source_id`, `occurred_at` and `fields->>'audit_action'` etc. on a few hundred to a few hundred
thousand rows; no new index in this step. If a count is slow on real data, add expression indexes on the `audit_*` keys
(a migration) then; that is the stated upgrade path, not a surprise.

## Testing and evaluation

- Unit and integration tests (pytest, real test database), written first, against the audit fixture
  (`tests/fixtures/projects/wso2-audit`, 80 events with known counts): every case of the audit golden set computed through
  `count_by` directly (e.g. `by=[application], action=Login, result=Success` gives My Account 10, Console 7, portalApplication 3;
  `by=[day, result]` gives the 6/2, 9/1, 5/2 table; `by=[user]` gives `groups` = 5 users); the time window in the project
  timezone (10:00-12:00 IST on 4 November gives 5); hidden users appear as codes and the note is added; a field no event has;
  `top` cut and the "more groups" note; case-insensitive filters; a hostile argument (SQL text in a filter) is only a value;
  the tool never reads another project's rows; `by` outside the enum is refused by the schema.
- Registry tests: `count_by` is in `TOOLS`, `BUDGETS`, and the graph's budget middleware refuses a third call.
- Agent evaluation (spends Groq quota, run by the user or with their go-ahead): the audit golden set before and after (baseline
  5/10 real) and the original 17-case set, to confirm no regression from the new tool and prompt 4.2. First fix the scorer:
  its "a bare number must stand alone" rule counted the 10 in a time such as `10:10:00Z`, which is why a wrong a4 answer
  scored as a pass (`scripts/eval_scoring.py` `_cited`); a number followed by `:digit` or `.digit` is not a standalone number.

## Risks and decisions to confirm

- Model may still prefer `overview` for counts. Mitigation: the description's wording and the eval; if it persists, the next step
  is a stronger sentence in the prompt, not a code change.
- One tool name `count_by` and the dimension names above are my choice; they are cheap to rename before building.
- Counting is over events, not people-sessions: a login is one `Login` event. Stated in the description so the answer says so.

## Non-goals

Arbitrary SQL or free-text fields, percentages or trends, distinct counts beyond the `groups` figure, new fields beyond step 1,
expression indexes, other log types' fields, charts or exports.
