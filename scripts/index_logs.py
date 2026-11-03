"""Index project log files into PostgreSQL. Read-only against the logs; only files in project folders under the
logs root (LOGS_ROOT) are read.

    python -m scripts.index_logs --project wso2-gateway
    python -m scripts.index_logs --file wso2-gateway/wso2carbon-10-30-2026.1
    python -m scripts.index_logs --all --reembed
    python -m scripts.index_logs --all --interval 60     # poll until Ctrl-C

Exit codes: 0 done (or stopped with Ctrl-C), 1 a file ended in error (one-shot mode), 2 bad arguments or project.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError
from app.ingestion.sources import ConfigError
from app.ingestion.pipeline import IndexReport, SessionFactory, index_source, reembed_missing
from app.ingestion.projects import Project, find_project, scan_projects
from app.ingestion.sources import Source

USAGE_ERROR = 2
Sleep = Callable[[float], Awaitable[None]]


def _positive(kind: type, allow_zero: bool = False) -> Callable[[str], int | float]:
    def parse(text: str) -> int | float:
        value = kind(text)
        if value < 0 or (value == 0 and not allow_zero):
            raise argparse.ArgumentTypeError(f"must be {'>= 0' if allow_zero else '> 0'}, got {text}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--project", help="every file of one project (its folder name)")
    which.add_argument("--file", help="one file, as PROJECT/STEM, e.g. wso2-gateway/wso2carbon-10-30-2026.1")
    which.add_argument("--all", action="store_true", help="every file of every valid project")
    parser.add_argument("--reembed", action="store_true", help="afterwards, embed rows stored without a vector")
    parser.add_argument("--interval", type=_positive(float), help="re-run every SECONDS until Ctrl-C")
    parser.add_argument("--max-events", type=_positive(int), default=200, help="events per batch (default 200)")
    parser.add_argument("--settle", type=_positive(float, allow_zero=True), default=5.0,
                        help="seconds a file must be quiet before its newest event counts as finished (default 5)")
    parser.add_argument("--logs-root", type=Path, help="folder of project folders (default: LOGS_ROOT or ./logs)")
    return parser


def _summary(report: IndexReport) -> str:
    line = (f"{report.source_id}: {report.status} - read {report.events_read}, stored {report.events_stored}, "
            f"embedded {report.embedded}, embed failed {report.embed_failed}, batches {report.batches}")
    return line + (f", error {report.error}" if report.error else "")


async def _run_once(args: argparse.Namespace, sources: tuple[Source, ...], factory: SessionFactory) -> int:
    reports = [
        await index_source(source, factory, max_events=args.max_events, settle_seconds=args.settle)
        for source in sources
    ]
    for report in reports:
        print(_summary(report))
    if args.reembed:
        targets = [None] if args.all else [source.id for source in sources]
        print(f"re-embedded {sum([await reembed_missing(factory, target) for target in targets])} rows")
    return 1 if any(report.status == "error" for report in reports) else 0


async def _run(args: argparse.Namespace, sources: tuple[Source, ...], factory: SessionFactory, sleep: Sleep) -> int:
    while True:
        code = await _run_once(args, sources, factory)
        if args.interval is None:
            return code
        await sleep(args.interval)


def main(argv: list[str] | None = None, *, session_factory: SessionFactory | None = None, sleep: Sleep = asyncio.sleep) -> int:
    args = build_parser().parse_args(argv)
    try:
        selected = _select(args, scan_projects(args.logs_root or get_settings().logs_root))
    except AppError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return USAGE_ERROR
    run = _run(args, selected, session_factory, sleep) if session_factory else _run_on_app_database(args, selected, sleep)
    try:
        return asyncio.run(run)
    except KeyboardInterrupt:
        return 0


def _select(args: argparse.Namespace, projects: tuple[Project, ...]) -> tuple[Source, ...]:
    """The files to index. --all skips invalid projects (and says so); naming one is an error."""
    if args.all:
        for project in projects:
            if project.error:
                print(f"skipping project {project.id}: {project.error}", file=sys.stderr)
        return tuple(source for project in projects for source in project.files)
    if args.file and "/" not in args.file:
        raise NotFoundError(f"Unknown file '{args.file}': name it as PROJECT/STEM")
    project_id, _, _ = (args.project or args.file).partition("/")
    project = find_project(projects, project_id)
    if project.error:
        raise ConfigError(f"Project '{project.id}' is invalid: {project.error}")
    if args.project:
        return project.files
    for source in project.files:
        if source.id == args.file:
            return (source,)
    raise NotFoundError(f"Unknown file '{args.file}': not in project '{project.id}'")


async def _run_on_app_database(args: argparse.Namespace, sources: tuple[Source, ...], sleep: Sleep) -> int:
    from app.core import database  # imported here: it creates the application engine on import

    try:
        return await _run(args, sources, database.SessionLocal, sleep)
    finally:
        await database.engine.dispose()  # close pooled connections while their event loop still runs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
