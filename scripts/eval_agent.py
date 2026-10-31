"""Run the agent golden set (tests/fixtures/agent-golden.yaml) against the real model and the indexed database.

    uv run python -m scripts.eval_agent                     all cases; results to docs/agent-eval-golden-<time>.json
    uv run python -m scripts.eval_agent --only s1-dev-auth-error-count,q2-identifier-hibernate-code
    uv run python -m scripts.eval_agent --class no_tool
    uv run python -m scripts.eval_agent --langsmith         also record an experiment in LangSmith

Needs the four fixture sources from sources.yaml indexed (scripts/index_logs.py). The two setups need no database
changes: `unindexed_source` adds a source that was never indexed, `stale_dev_auth` points dev-auth-service at a newer
copy of its file. Paced for Groq's free tier (8,000 tokens a minute): one case at a time, a pause after each, a retry
after a failure. With LANGSMITH_TRACING on, questions, tool results (log text) and answers go to LangSmith: fixture
logs only.
"""

import argparse
import asyncio
import json
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import yaml

from app.agent.graph import ask, build_agent
from app.agent.tools import make_search_logs_tool, make_summarize_logs_tool
from app.controllers.dependencies import get_sources
from app.core.database import SessionLocal
from app.core.errors import AIUnavailableError
from app.ingestion.coverage import time_spans
from app.ingestion.sources import Source
from scripts.eval_scoring import score

GOLDEN = Path("tests/fixtures/agent-golden.yaml")
PAUSE = 30  # seconds between cases, so the per-minute token window drains
RETRY_WAIT = 70
ATTEMPTS = 3
MAX_RESULT_CHARS = 300  # of each tool result kept in the report, for debugging retrieval
STALE_LINE = "2026-03-10 15:45:00.000 [main] ERROR o.s.boot.SpringApplication - appended after indexing\n"


def sources_for(setup: str | None, base: tuple[Source, ...], workdir: Path) -> tuple[Source, ...]:
    if setup == "unindexed_source":
        demo = next(s for s in base if s.id == "demo-app")
        return (*base, replace(demo, id="payments-api"))
    if setup == "stale_dev_auth":
        auth = next(s for s in base if s.id == "dev-auth-service")
        copy = workdir / "dev-auth-service.log"
        shutil.copyfile(auth.path, copy)  # a fresh mtime, newer than the last indexing run: coverage says "stale"
        with copy.open("a", encoding="utf-8") as file:
            file.write(STALE_LINE)
        return tuple(replace(s, path=copy) if s is auth else s for s in base)
    return base


async def run_case(case: dict, base: tuple[Source, ...], workdir: Path) -> dict:
    sources = sources_for(case.get("setup"), base, workdir)
    async with SessionLocal() as session:
        spans = await time_spans(session, sources)
    agent = build_agent(
        sources=sources, spans=spans, tool=make_search_logs_tool(load_sources=lambda: sources),
        summary_tool=make_summarize_logs_tool(load_sources=lambda: sources))
    started = time.monotonic()
    for attempt in range(1, ATTEMPTS + 1):
        try:
            reply = await ask(agent, case["question"])
            break
        except AIUnavailableError:
            if attempt == ATTEMPTS:
                return {"answer": "", "tool_calls": [], "error": "AI unavailable", "attempts": attempt}
            await asyncio.sleep(RETRY_WAIT)
    calls = [
        {"name": c.name, "args": c.args, "status": c.status, "result": (c.result or "")[:MAX_RESULT_CHARS]}
        for c in reply.tool_calls]
    return {"answer": reply.answer, "tool_calls": calls, "attempts": attempt,
            "seconds": round(time.monotonic() - started, 1)}


def record(case: dict, outputs: dict) -> dict:
    checks = score(case, outputs["answer"], outputs["tool_calls"])
    failed = [name for name, ok in checks.items() if not ok and name != "passed"]
    print(f"{'PASS' if checks['passed'] else 'FAIL'}  {case['id']:<32} {', '.join(failed)}", flush=True)
    return {"id": case["id"], "class": case["class"], "kind": case["kind"], "question": case["question"],
            **outputs, "checks": checks}


async def run_local(cases: Sequence[dict], pause: float, workdir: Path) -> list[dict]:
    base, results = get_sources(), []
    for n, case in enumerate(cases):
        results.append(record(case, await run_case(case, base, workdir)))
        if n < len(cases) - 1:
            await asyncio.sleep(pause)
    return results


async def run_langsmith(cases: Sequence[dict], all_cases: Sequence[dict], version: int, pause: float,
                        workdir: Path) -> list[dict]:
    from langsmith import Client, aevaluate

    client, name = Client(), f"agent-golden-v{version}"
    if not client.has_dataset(dataset_name=name):  # bump `version` in the YAML after editing cases
        dataset = client.create_dataset(name, description=f"Agent golden set from {GOLDEN}")
        client.create_examples(dataset_id=dataset.id, examples=[
            {"inputs": {"id": c["id"], "question": c["question"]}, "outputs": c,
             "metadata": {"class": c["class"], "kind": c["kind"]}} for c in all_cases])
    wanted = {c["id"] for c in cases}
    examples = [e for e in client.list_examples(dataset_name=name) if e.inputs["id"] in wanted]
    by_id, base, results = {c["id"]: c for c in cases}, get_sources(), []

    async def target(inputs: dict) -> dict:
        case = by_id[inputs["id"]]
        outputs = await run_case(case, base, workdir)
        results.append(record(case, outputs))
        await asyncio.sleep(pause)
        return outputs

    def checks(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
        result = score(reference_outputs, outputs.get("answer") or "", outputs.get("tool_calls") or [])
        return {"results": [{"key": key, "score": int(ok)} for key, ok in result.items()]}

    await aevaluate(target, data=examples, evaluators=[checks], experiment_prefix="agent-golden", max_concurrency=1,
                    metadata={"golden_version": version, "cases": len(examples)})
    return results


def report(results: Sequence[dict]) -> None:
    by_class, failures = defaultdict(Counter), Counter()
    for r in results:
        by_class[r["class"]]["passed" if r["checks"]["passed"] else "failed"] += 1
        failures.update(name for name, ok in r["checks"].items() if not ok and name != "passed")
    print("\nclass                  passed / total")
    for name, counts in by_class.items():
        print(f"  {name:<20} {counts['passed']:>3} / {counts['passed'] + counts['failed']}")
    total = sum(r["checks"]["passed"] for r in results)
    print(f"  {'all':<20} {total:>3} / {len(results)}")
    if failures:
        print("failed checks: " + ", ".join(f"{name} x{n}" for name, n in failures.most_common()))


def select(cases: list[dict], only: str | None, cls: str | None) -> list[dict]:
    ids = set(only.split(",")) if only else None
    chosen = [c for c in cases if (ids is None or c["id"] in ids) and (cls is None or c["class"] == cls)]
    unknown = ids - {c["id"] for c in cases} if ids else set()
    if unknown:
        raise SystemExit(f"unknown case ids: {', '.join(sorted(unknown))}")
    return chosen


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--class", dest="cls", help="run one class only")
    parser.add_argument("--pause", type=float, default=PAUSE, help="seconds between cases")
    parser.add_argument("--langsmith", action="store_true", help="record an experiment in LangSmith")
    parser.add_argument("--out", type=Path, help="results file (default docs/agent-eval-golden-<time>.json)")
    args = parser.parse_args()

    golden = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    cases = select(golden["cases"], args.only, args.cls)
    with tempfile.TemporaryDirectory() as tmp:
        if args.langsmith:
            results = await run_langsmith(cases, golden["cases"], golden["version"], args.pause, Path(tmp))
        else:
            results = await run_local(cases, args.pause, Path(tmp))
    out = args.out or Path(f"docs/agent-eval-golden-{datetime.now():%Y%m%d-%H%M}.json")
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    report(results)
    print(f"results: {out}")


if __name__ == "__main__":
    asyncio.run(main())
