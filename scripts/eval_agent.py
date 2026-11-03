"""Run the agent golden set (tests/fixtures/agent-golden.yaml) against the real model and the indexed database.

    uv run python -m scripts.eval_agent                     all cases; results to docs/<golden file>-eval-<time>.json
    uv run python -m scripts.eval_agent --smoke             the smoke set (one case per class), after a prompt change
    uv run python -m scripts.eval_agent --only s1-dev-auth-error-count,q2-identifier-hibernate-code
    uv run python -m scripts.eval_agent --class no_tool
    uv run python -m scripts.eval_agent --langsmith         also record an experiment in LangSmith

Needs the project (default wso2-gateway, under LOGS_ROOT) indexed (scripts/index_logs.py --project ...). Paced for Groq's free tier (8,000 tokens a minute): one case at a time, a pause after each, a retry
after a failure. With LANGSMITH_TRACING on, questions, tool results (log text) and answers go to LangSmith: fixture
logs only.
"""

import argparse
import asyncio
import json
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import yaml

from app.agent.graph import ask, build_agent
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.errors import AIUnavailableError
from app.ingestion.coverage import time_spans
from app.ingestion.projects import Project, find_project, scan_projects
from scripts.eval_scoring import score

GOLDEN = Path("tests/fixtures/agent-golden.yaml")
PROJECT = "wso2-gateway"
PAUSE = 30  # seconds between cases, so the per-minute token window drains
RETRY_WAIT = 70
ATTEMPTS = 3
MAX_RESULT_CHARS = 300  # of each tool result kept in the report, for debugging retrieval


async def run_case(case: dict, project: Project) -> dict:
    if case.get("setup"):
        raise SystemExit(f"{case['id']}: setups are not supported since projects replaced sources.yaml")
    async with SessionLocal() as session:
        spans = await time_spans(session, project.files)
    agent = build_agent(project=project, spans=spans)
    started = time.monotonic()
    for attempt in range(1, ATTEMPTS + 1):
        try:
            reply = await ask(agent, case["question"])
            break
        except AIUnavailableError as exc:
            # 413: one request is bigger than the per-minute token limit, so waiting and retrying cannot help
            too_large = getattr(exc.__cause__, "status_code", None) == 413
            if too_large or attempt == ATTEMPTS:
                error = "request too large for the token limit" if too_large else "AI unavailable"
                return {"answer": "", "tool_calls": [], "error": error, "attempts": attempt}
            await asyncio.sleep(RETRY_WAIT)
    calls = [
        {"name": c.name, "turn": c.turn, "args": c.args, "status": c.status,
         "result": (c.result or "")[:MAX_RESULT_CHARS]}
        for c in reply.tool_calls]
    # every turn that asked for tools, plus the final answer; fewer model calls = fewer tokens (ADR-003)
    model_calls = len({c.turn for c in reply.tool_calls}) + 1
    return {"answer": reply.answer, "tool_calls": calls, "model_calls": model_calls, "attempts": attempt,
            "seconds": round(time.monotonic() - started, 1)}


def record(case: dict, outputs: dict) -> dict:
    checks = score(case, outputs["answer"], outputs["tool_calls"])
    failed = [name for name, ok in checks.items() if not ok and name != "passed"]
    calls = f"{len(outputs['tool_calls'])} tools / {outputs.get('model_calls', '?')} model calls"
    print(f"{'PASS' if checks['passed'] else 'FAIL'}  {case['id']:<32} {calls:<26} {', '.join(failed)}", flush=True)
    return {"id": case["id"], "class": case["class"], "kind": case["kind"], "question": case["question"],
            **outputs, "checks": checks}


async def run_local(cases: Sequence[dict], pause: float, project: Project) -> list[dict]:
    results = []
    for n, case in enumerate(cases):
        results.append(record(case, await run_case(case, project)))
        if n < len(cases) - 1:
            await asyncio.sleep(pause)
    return results


async def run_langsmith(cases: Sequence[dict], all_cases: Sequence[dict], name: str, pause: float,
                        project: Project) -> list[dict]:
    from langsmith import Client, aevaluate

    client = Client()
    if not client.has_dataset(dataset_name=name):  # bump `version` in the YAML after editing cases
        dataset = client.create_dataset(name, description=f"Agent golden set {name}")
        client.create_examples(dataset_id=dataset.id, examples=[
            {"inputs": {"id": c["id"], "question": c["question"]}, "outputs": c,
             "metadata": {"class": c["class"], "kind": c["kind"]}} for c in all_cases])
    wanted = {c["id"] for c in cases}
    examples = [e for e in client.list_examples(dataset_name=name) if e.inputs["id"] in wanted]
    by_id, results = {c["id"]: c for c in cases}, []

    async def target(inputs: dict) -> dict:
        case = by_id[inputs["id"]]
        outputs = await run_case(case, project)
        results.append(record(case, outputs))
        await asyncio.sleep(pause)
        return outputs

    def checks(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
        result = score(reference_outputs, outputs.get("answer") or "", outputs.get("tool_calls") or [])
        return {"results": [{"key": key, "score": int(ok)} for key, ok in result.items()]}

    await aevaluate(target, data=examples, evaluators=[checks], experiment_prefix="agent-golden", max_concurrency=1,
                    metadata={"golden": name, "cases": len(examples)})
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


def select(cases: list[dict], only: str | None, cls: str | None, smoke: bool = False) -> list[dict]:
    ids = set(only.split(",")) if only else None
    chosen = [c for c in cases if (ids is None or c["id"] in ids) and (cls is None or c["class"] == cls)
              and (not smoke or c.get("smoke"))]
    unknown = ids - {c["id"] for c in cases} if ids else set()
    if unknown:
        raise SystemExit(f"unknown case ids: {', '.join(sorted(unknown))}")
    return chosen


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--class", dest="cls", help="run one class only")
    parser.add_argument("--smoke", action="store_true", help="only the smoke set (one case per class)")
    parser.add_argument("--golden", type=Path, default=GOLDEN, help=f"golden set file (default {GOLDEN})")
    parser.add_argument("--pause", type=float, default=PAUSE, help="seconds between cases")
    parser.add_argument("--langsmith", action="store_true", help="record an experiment in LangSmith")
    parser.add_argument("--project", default=PROJECT, help=f"project folder under LOGS_ROOT (default {PROJECT})")
    parser.add_argument("--out", type=Path, help="results file (default docs/agent-eval-golden-<time>.json)")
    args = parser.parse_args()

    golden = yaml.safe_load(args.golden.read_text(encoding="utf-8"))
    name = f"{args.golden.stem}-v{golden['version']}"  # LangSmith dataset; bump `version` after editing cases
    cases = select(golden["cases"], args.only, args.cls, args.smoke)
    project = find_project(scan_projects(get_settings().logs_root), args.project)
    if project.error:
        raise SystemExit(f"project {project.id} is invalid: {project.error}")
    if args.langsmith:
        results = await run_langsmith(cases, golden["cases"], name, args.pause, project)
    else:
        results = await run_local(cases, args.pause, project)
    out = args.out or Path(f"docs/{args.golden.stem}-eval-{datetime.now():%Y%m%d-%H%M}.json")
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    report(results)
    print(f"results: {out}")


if __name__ == "__main__":
    asyncio.run(main())
