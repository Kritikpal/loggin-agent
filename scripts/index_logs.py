"""Index allowlisted log sources into PostgreSQL. Read-only against the logs; only sources.yaml entries are read.

    python -m scripts.index_logs --source demo-app
    python -m scripts.index_logs --all --reembed
    python -m scripts.index_logs --all --interval 60     # poll until Ctrl-C

Exit codes: 0 done (or stopped with Ctrl-C), 1 a source ended in error (one-shot mode), 2 bad arguments or config.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError
from app.ingestion.pipeline import IndexReport, SessionFactory, index_source, reembed_missing
from app.ingestion.sources import Source, get_source, load_sources

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
    which.add_argument("--source", help="the id of one source in sources.yaml")
    which.add_argument("--all", action="store_true", help="every source in sources.yaml")
    parser.add_argument("--reembed", action="store_true", help="afterwards, embed rows stored without a vector")
    parser.add_argument("--interval", type=_positive(float), help="re-run every SECONDS until Ctrl-C")
    parser.add_argument("--max-events", type=_positive(int), default=200, help="events per batch (default 200)")
    parser.add_argument("--settle", type=_positive(float, allow_zero=True), default=5.0,
                        help="seconds a file must be quiet before its newest event counts as finished (default 5)")
    parser.add_argument("--config", type=Path, help="sources file (default: SOURCES_CONFIG_PATH or sources.yaml)")
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
        print(f"re-embedded {await reembed_missing(factory, args.source)} rows")
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
        sources = load_sources(args.config or get_settings().sources_config_path)
        selected = sources if args.all else (get_source(sources, args.source),)
    except AppError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return USAGE_ERROR
    run = _run(args, selected, session_factory, sleep) if session_factory else _run_on_app_database(args, selected, sleep)
    try:
        return asyncio.run(run)
    except KeyboardInterrupt:
        return 0


async def _run_on_app_database(args: argparse.Namespace, sources: tuple[Source, ...], sleep: Sleep) -> int:
    from app.core import database  # imported here: it creates the application engine on import

    try:
        return await _run(args, sources, database.SessionLocal, sleep)
    finally:
        await database.engine.dispose()  # close pooled connections while their event loop still runs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
