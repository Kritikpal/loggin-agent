# `count_by` Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent answers "how many / who / which the most / per day / how many different" questions about WSO2 audit events with an exact SQL count from a new `count_by` tool, instead of counting a sample of 25 lines.

**Architecture:** A new tool file `app/agent/tools/count_by.py` (a fixed vocabulary of dimensions mapped to SQL expressions over `log_events.fields`, `level` and `occurred_at`; bound parameters only) is registered in the existing tool registry with a budget and a digest. The tool description carries the use-when rule; prompt 4.2 adds one citing rule. A scorer bug that let a wrong answer pass is fixed first.

**Tech Stack:** Python 3.13, SQLAlchemy async (`text()` SQL), LangChain `StructuredTool` (via `app.agent.tools.common.tool`), pydantic, pytest (`integration` mark for DB tests).

**Spec:** `docs/superpowers/specs/2026-11-06-count-by-tool-design.md` (builds on `2026-11-05-audit-field-extraction-design.md`)

## Global Constraints

- Tool name `count_by`; `BUDGET = 2` calls per question; the shared cap `MAX_TOOL_CALLS = 4` is unchanged.
- `by`: a list of 1 to 2 values of `action, result, user, actor, application, target, level, day, hour` (an enum; anything else is refused by the schema). Filters: `action, result, application, user` (exact, case-insensitive), `start_time`, `end_time` (same help text and handling as the other tools, `time_window`), `top` default 15, clipped to a maximum of 50.
- SQL: constants only for identifiers/expressions; every value (filters, window, `top`, timezone) is a bound parameter. Read-only SELECTs through `SessionLocal`. Only `project.sources` ids are counted.
- Dimension expressions: `action`/`result`/`actor`/`application`/`target` = `fields->>'audit_<name>'`; `user` = `coalesce(fields->>'audit_user', fields->>'audit_actor')`; `level` = the `level` column; `day` = `to_char(occurred_at at time zone :tz, 'YYYY-MM-DD')`; `hour` = `to_char(occurred_at at time zone :tz, 'YYYY-MM-DD HH24:00')` (project timezone).
- `total` = events counted in the groups (matching the filters and having every chosen dimension); `groups` = distinct groups; `missing` = matching events lacking a chosen dimension (reported in the header, not a group).
- Output: header line, one `key | ... | count | first | last` line per group (largest first, ties by key), then the `fitted(...)` note and `coverage_line(...)` footer. `first`/`last` use the `L<line>@<time>` form of `overview`.
- Hidden-user note (fixed text) is added when any shown value starts with `[REDACTED:email`.
- Errors: unexpected failure returns `error: count_by is temporarily unavailable` and is logged; a requested dimension that no event in scope has returns a clear sentence, not an empty table.
- Prompt version 4.2; `len(PROMPT) <= 2300` stays true.
- Run tests with `.venv/Scripts/python -m pytest tests -q --ignore=tests/test_agent_graph.py` (that file has 18 older failures). A dev `PSEUDONYM_KEY` may be set in `.env`: tests must accept both `[REDACTED:email]` and `[REDACTED:email:u...]` for hidden users.

## Review Focus

- Hostile filter values (`x'; drop table log_events;--`, `%`, very long strings) are only values: no error, no effect, table intact (Task 1).
- The tool never counts another project's rows, even for the same field values (Task 1).
- `day` and `hour` boundaries follow the project timezone, not UTC (an event at 23:30 IST on 4 Nov is 4 Nov; at 00:10 IST on 5 Nov is 5 Nov) (Task 1).
- Events lacking the field are `missing`, not a group; zero matches say `total 0` and, with incomplete coverage, never read as "none" (Tasks 1 and 2).
- A cross table with many groups (`by=[day, user]`) is cut to `top` with a "more groups" note and stays within the result size cap (Tasks 1 and 2).

## File Structure

- Create `app/agent/tools/count_by.py`: vocabulary, `group_counts`, `render_counts`, `make_count_by_tool`.
- Modify `app/agent/tools/overview.py`: rename private `_cite` to public `cite` (used by both tools).
- Modify `app/agent/tools/format.py`: `digest_count`.
- Modify `app/agent/tools/__init__.py`: register `ToolSpec("count_by", ...)`.
- Modify `app/agent/prompt.py`: version 4.2 and one citing rule.
- Modify `scripts/eval_scoring.py`: `BUDGETS["count_by"] = 2`; fix `_cited`.
- Modify `tests/test_agent_golden.py` (tools union), `tests/test_agent_budget.py`, `tests/test_agent_prompt.py`; `tests/fixtures/agent-golden-audit.yaml` (`tools: [count_by]` on a5, a6, a7).
- Tests: `tests/test_count_by.py` (new), plus the files above.

---

### Task 1: Scorer fix and the counting query

**Files:**
- Modify: `scripts/eval_scoring.py` (`_cited`)
- Create: `app/agent/tools/count_by.py` (query part)
- Test: `tests/test_eval_scoring.py` (create or append), `tests/test_count_by.py`

**Interfaces:**
- Produces: `DIMENSIONS: Mapping[str, str]` (name -> SQL expression, constants); `FILTERS: tuple[str, ...] = ("action", "result", "application", "user")`; frozen dataclasses `Group(key: tuple[str, ...], n: int, first_file: str, first_line: int | None, first_at: datetime | None, last_file: str, last_line: int | None, last_at: datetime | None)` and `Counts(total: int, groups: int, missing: int, rows: tuple[Group, ...])`; `async def group_counts(session: AsyncSession, *, source_ids: Sequence[str], by: Sequence[str], filters: Mapping[str, str], start: datetime | None, end: datetime | None, timezone: str, top: int) -> Counts`.

- [ ] **Step 1: Write failing tests.**
  - Scorer (`tests/test_eval_scoring.py`): `test_a_time_is_not_a_standalone_number`: `_cited(10, "last at 10:10:00Z")` is False, `_cited(10, "10 logins")` True, `_cited(10, "10.")` True, `_cited(10, "10.5 s")` False; `test_the_wrong_a4_answer_no_longer_passes`: score the saved baseline answer from `docs/eval-runs/agent-golden-audit-v1-baseline.json` (case id `a4-most-accessed-application`, load with `json`; skip the test if the file is missing) against the case in `agent-golden-audit.yaml`: `checks["answer_has"]` is False.
  - Query (`tests/test_count_by.py`, `pytestmark = pytest.mark.integration`): a module fixture indexes `tests/fixtures/projects/wso2-audit` into the test database (as `tests/test_agent_golden_audit.py::test_the_indexed_fixture_gives_the_same_counts_in_sql` does with `index_file`) and a helper `counts(session_factory, by, **kw)` calls `group_counts` with the indexed source id, timezone `Asia/Kolkata`, `top=50`. Tests: `test_logins_by_application` (`by=["application"]`, filters `action=Login, result=Success`) -> rows `[("My Account",10),("Console",7),("portalApplication",3)]`, `total 20`, `groups 3`; `test_failed_logins_by_application` -> `portalApplication 4`, `Console 1`; `test_users_by_successful_logins` -> first row `("Goutam001",8)`, second `("admin",6)`, `groups 5`, and exactly one group key starts with `[REDACTED:email` with count 2; `test_day_and_result_cross_table` (`by=["day","result"]`, `action=Login`) -> `("2026-11-03","Success")=6, ("2026-11-03","Failed")=2, ("2026-11-04","Success")=9, ("2026-11-04","Failed")=1, ("2026-11-05","Success")=5, ("2026-11-05","Failed")=2`; `test_the_most_frequent_action` (`by=["action"]`) -> `Get-User-Claim-Values 31`, `Login 25`; `test_time_window_in_project_timezone` (start `2026-11-04T04:30:00Z`, end `2026-11-04T06:30:00Z`, `action=Login, result=Success`) -> `total 5`; `test_filters_are_case_insensitive` (`action="login"`, `result="SUCCESS"`) -> `total 20`; `test_events_without_the_field_are_missing_not_a_group` (`by=["application"]`, no filters) -> `missing` > 0, no group key empty, and `total + missing` equals the number of audit events; `test_top_cuts_the_rows_but_not_groups` (`top=2`) -> `len(rows) == 2`, `groups == 3`; `test_hostile_filter_values_are_only_values` (`user="x'; drop table log_events;--"`, `application="%"`) -> `total 0` and a following count query still works; `test_day_boundary_uses_the_project_timezone`: store two extra Login events directly (`upsert_events` with `fields` containing `audit_action="Login"`, `occurred_at` `2026-11-04T18:00:00Z` = 23:30 IST on 4 Nov, and `2026-11-04T18:40:00Z` = 00:10 IST on 5 Nov) under a separate source id and check `by=["day"]` puts them on `2026-11-04` and `2026-11-05`; `test_other_sources_are_not_counted` (an extra Login event under another source id is absent from the result); `test_level_and_hour_dimensions_work_without_audit_fields` (`by=["level"]` on the fixture's non-audit lines gives `INFO`/`WARN`, `by=["hour"]` keys look like `2026-11-04 10:00`).
- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest tests/test_eval_scoring.py tests/test_count_by.py -q`. Expected: FAIL (`_cited` accepts the time; module missing).
- [ ] **Step 3: Implement.** `_cited` regex becomes `(?<![\d.:,]){line}(?!\d|[:.]\d)`. In `count_by.py` write `group_counts`: build the WHERE with `Scope(tuple(source_ids), start, end, None).where()` (from `overview.py`), add one `lower(<expr>) = lower(:f_<name>)` per filter, select the dimension expressions as `k0, k1` plus `count(*)`, `count(*) over ()` is not needed: compute `groups` with a second aggregate over the grouped subquery; `first_*`/`last_*` with `array_agg(... order by occurred_at, source_id, line)` exactly as `kind_rows` does; `missing` as a separate `count(*) filter (where <any chosen expr> is null)` over the same WHERE. Group rows ordered by `n desc, k0, k1`, limited to `:top`. `top` is clipped to 50 by the caller. Never format values into SQL.
- [ ] **Step 4: Run** the new tests and the whole suite. Expected: PASS; existing scorer-based tests (`tests/test_agent_golden.py`, `tests/test_eval_scoring*.py`) still pass.
- [ ] **Step 5: Commit** `feat: count_by query and a scorer fix for times read as numbers`.

### Task 2: The tool wrapper and its output

**Files:**
- Modify: `app/agent/tools/count_by.py`, `app/agent/tools/overview.py` (`_cite` -> `cite`)
- Test: `tests/test_count_by.py` (append)

**Interfaces:**
- Consumes: `group_counts`, `Counts`, `Group` (Task 1); `tool`, `ToolArgs`, `time_window`, `project_timezone`, `fitted`, `TIME_HELP`, `END_HELP` (`app.agent.tools.common`); `coverage_line`, `short` (`format`); `Project`.
- Produces: `BUDGET = 2`; `DESCRIPTION: str`; `class CountByArgs(ToolArgs)` (fields as in Global Constraints; `by` is `list[Dimension]` with `min_length=1, max_length=2` where `Dimension = Literal[...]`); `def render_counts(counts: Counts, *, by: Sequence[str], filters: Mapping[str, str], window: tuple[datetime | None, datetime | None], project_id: str, coverage: str, top: int) -> str`; `def make_count_by_tool(project: Project) -> StructuredTool`.

- [ ] **Step 1: Write failing tests.** Pure render tests (no DB): `test_header_has_window_filters_by_total_groups_and_columns` (the header contains `by application`, `filters action=Login result=Failed`, `total 5`, `groups 2`, `columns: application | count | first | last`); `test_rows_are_pipe_separated_with_citations` (`portalApplication | 4 | L20@11-03T07:42Z | L72@11-05T06:45Z`); `test_missing_events_are_reported_in_the_header` (`3 events have no application`); `test_hidden_users_get_the_fixed_note` (a key `[REDACTED:email:u5b4b6d76b6c]` adds the note once; none for ordinary names); `test_more_groups_note` (`groups 7`, 2 rows -> `5 more groups; raise top (max 50) or narrow the window`); `test_zero_matches` (`total 0`, coverage footer present); `test_a_field_no_event_has` (`counts.total == 0 and counts.missing > 0` and no group -> sentence `no events in this window have application`). Tool tests (integration, fixture project from Task 1 wrapped by `make_project`/a real scanned `Project`): `test_the_tool_answers_failed_logins_by_application` (`await tool.ainvoke({"by": ["application"], "action": "Login", "result": "Failed"})` contains `portalApplication | 4`); `test_an_unknown_dimension_is_refused` (`by=["password"]` -> string starting `error:` and containing `by`); `test_three_dimensions_are_refused`; `test_top_above_the_maximum_is_clipped` (`top=500` runs, at most 50 rows); `test_the_tool_only_counts_its_projects_sources` (a second project with another source id does not leak); `test_a_database_failure_is_a_clean_error` (monkeypatch `app.agent.tools.count_by.SessionLocal` to raise; result is `error: count_by is temporarily unavailable`); `test_many_groups_are_cut_to_the_size_cap` (`by=["day","user"]` with `top=50` stays within `MAX_RESULT_CHARS`, the cut noted).
- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest tests/test_count_by.py -q`. Expected: FAIL (names missing).
- [ ] **Step 3: Implement.** `DESCRIPTION` must say: counts events by one or two fields (user, application, action, result, day, hour, level, target, actor); use for how many logins or failures, who or which application the most, how many different users (the `groups` figure) and a report per day; counts are exact so never count listed events yourself; an event is one log line, not a session; 2 calls per question. `make_count_by_tool` mirrors `make_overview_tool`: sources/timezone from the project, `time_window`, `top = min(arguments.get("top", 15), 50)`, filters dict from the non-null filter arguments, coverage from `coverage_for_all`, errors logged and returned as the fixed sentence. Rename `_cite` to `cite` in `overview.py` and use it for `first`/`last`. Render with `fitted(header, lines, note, coverage, what="rows")`.
- [ ] **Step 4: Run** the tests and the whole suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat: count_by agent tool`.

### Task 3: Registry, budget and digest

**Files:**
- Modify: `app/agent/tools/__init__.py`, `app/agent/tools/format.py`, `scripts/eval_scoring.py` (`BUDGETS`)
- Test: `tests/test_agent_budget.py`, `tests/test_agent_golden.py`, `tests/test_count_by.py`

**Interfaces:**
- Consumes: `make_count_by_tool`, `BUDGET` (Task 2).
- Produces: `digest_count(result: str) -> str | None` in `format.py` (header plus the first 10 group lines marked with `DIGEST_MARK`; `None` for errors, results already digested, or results of at most 12 lines); `TOOLS` contains `ToolSpec("count_by", make_count_by_tool, BUDGET, digest_count)`; `scripts.eval_scoring.BUDGETS == {"overview": 1, "find": 2, "search_logs": 2, "count_by": 2}`.

- [ ] **Step 1: Write failing tests.** `test_count_by_is_registered_with_a_budget_of_two` (`from app.agent.tools import BUDGETS, TOOLS`; names, `BUDGETS["count_by"] == 2`, `MAX_TOOL_CALLS == 4`); `test_the_scorer_budgets_match_the_registry` (`scripts.eval_scoring.BUDGETS == app.agent.tools.BUDGETS`); in `tests/test_agent_budget.py` a third `count_by` call is refused by `ToolBudget` built from the registry `BUDGETS` and a failed (`error`) call costs nothing; `test_digest_count_keeps_the_header_and_ten_rows` (a 30-line result becomes 12 lines: mark, header, 10 rows; an `error:` result, a short result and an already-digested result give `None`). Update `tests/test_agent_golden.py::test_every_tool_is_required_by_some_case` to compute `required` from both `agent-golden.yaml` and `agent-golden-audit.yaml`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** the registry entry, `digest_count`, the scorer budget, and set `tools: [count_by]` on cases a5, a6 and a7 of `tests/fixtures/agent-golden-audit.yaml` (their answers cannot be got another way; the other cases keep `tools: []`). Keep `test_agent_golden_audit.py` passing.
- [ ] **Step 4: Run** the whole suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat: register count_by with budget and digest`.

### Task 4: Prompt 4.2

**Files:**
- Modify: `app/agent/prompt.py`
- Test: `tests/test_agent_prompt.py`

- [ ] **Step 1: Write failing tests.** `PROMPT_VERSION == "4.2"`; `len(PROMPT) <= 2300`; the built prompt contains `count_by` and still contains `never count sampled hits`; the changelog docstring has a `4.2 (2026-11-06)` entry.
- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest tests/test_agent_prompt.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Extend the citing rule to "Cite a count by its sig, e.g. (sig 7dcd9c77), or a count_by count as (count_by <by>)." and add the changelog entry (4.2: the audit golden baseline was 5/10 real because no tool counted by field; one citing rule, about +40 tokens). Update the version constant and the existing assertion in the test.
- [ ] **Step 4: Run** the whole suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat: prompt 4.2 cites count_by counts`.

### Task 5: Docs and evaluation

**Files:**
- Modify: `README.md` (a short "Counting questions" paragraph), `tests/fixtures/agent-golden-audit.yaml` (header baseline note)

- [ ] **Step 1: Document** in `README.md` what `count_by` answers, its dimensions and filters, that counts are over events, the hidden-user note, and the golden-set run commands.
- [ ] **Step 2: Run** the full suite with coverage on `app.agent.tools.count_by`; the new module must reach at least 80%.
- [ ] **Step 3: Agent evaluation (needs the user's go-ahead; it spends Groq quota).** With `logs/wso2-audit` indexed (already done in the dev database), run `.venv/Scripts/python -m scripts.eval_agent --golden tests/fixtures/agent-golden-audit.yaml --project wso2-audit --workers 4 --pause 15 --out docs/eval-runs/agent-golden-audit-v2-count-by.json`, then the original set (`--project wso2-gateway`, default golden file) to check for regressions. The four keys share one Groq organisation limit, so expect retries. Compare with the baseline (5/10 real, v1) and `tests/eval-result-v4.json`; read the answers, not only the scorer.
- [ ] **Step 4: Update** the baseline comment in `agent-golden-audit.yaml` with the v2 result.
- [ ] **Step 5: Commit** `docs: count_by and the audit golden set result`.
