"""Fill in or refresh event signatures in place, without re-indexing: for events stored before signatures existed or
under older signature rules. Only the signature columns are written; no event is added or removed.

    python -m scripts.backfill_signatures                  every valid project under LOGS_ROOT
    python -m scripts.backfill_signatures --project wso2-gateway
"""

import argparse
import asyncio
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from sqlalchemy import select, update

from app.core.config import get_settings
from app.ingestion.projects import find_project, scan_projects
from app.ingestion.signature import signature_for
from app.ingestion.sources import Source
from app.models.log_event import LogEvent

BATCH_SIZE = 500


async def backfill(session_factory: Callable, sources: Sequence[Source], batch_size: int = BATCH_SIZE) -> int:
    """Recompute every event's signature; write only those that differ. Returns how many events changed."""
    changed = 0
    for source in sources:
        last_id: uuid.UUID | None = None
        while True:
            async with session_factory() as session, session.begin():
                query = select(LogEvent.id, LogEvent.raw_text, LogEvent.signature, LogEvent.signature_template).where(
                    LogEvent.source_id == source.id)
                if last_id is not None:
                    query = query.where(LogEvent.id > last_id)
                rows = (await session.execute(query.order_by(LogEvent.id).limit(batch_size))).all()
                if not rows:
                    break
                updates = [
                    {"id": row.id, "signature": new.sig, "signature_template": new.template}
                    for row in rows
                    if (new := signature_for(row.raw_text, source)) and (row.signature, row.signature_template)
                    != (new.sig, new.template)
                ]
                if updates:
                    await session.execute(update(LogEvent), updates)  # bulk UPDATE by primary key
                changed += len(updates)
                last_id = rows[-1].id
    return changed


async def _main(logs_root: Path, project_id: str | None) -> None:
    from app.core import database  # imported here: it creates the application engine on import

    projects = scan_projects(logs_root)
    chosen = [find_project(projects, project_id)] if project_id else [p for p in projects if not p.error]
    try:
        for project in chosen:
            for source in project.files:
                print(f"{source.id}: {await backfill(database.SessionLocal, [source])} events updated", flush=True)
    finally:
        await database.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", help="one project (default: every valid project)")
    parser.add_argument("--logs-root", type=Path, help="folder of project folders (default: LOGS_ROOT or ./logs)")
    args = parser.parse_args()
    asyncio.run(_main(args.logs_root or get_settings().logs_root, args.project))
