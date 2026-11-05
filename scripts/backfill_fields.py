"""Fill in or refresh the parsed `fields` of stored events in place, without re-indexing: for events stored before a field
(such as the WSO2 audit_* keys) existed. Only the fields column is written; text, embeddings and signatures are not touched,
and no event is added or removed. Pushed events are included, not only files.

    python -m scripts.backfill_fields                  every valid project under LOGS_ROOT
    python -m scripts.backfill_fields --project sagar-wso2

Addresses already masked before PSEUDONYM_KEY was set stay `[REDACTED:email]`: the address is gone and cannot be recovered.
"""

import argparse
import asyncio
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from sqlalchemy import select, update

from app.core.config import get_settings
from app.ingestion.fields import resolve
from app.ingestion.projects import Project, find_project, scan_projects
from app.ingestion.sources import Source
from app.models.log_event import LogEvent

BATCH_SIZE = 500


async def backfill(session_factory: Callable, sources: Sequence[Source], batch_size: int = BATCH_SIZE) -> int:
    """Recompute every event's fields from its stored text; write only those that differ. Returns how many changed."""
    changed = 0
    for source in sources:
        last_id: uuid.UUID | None = None
        while True:
            async with session_factory() as session, session.begin():
                query = select(LogEvent.id, LogEvent.raw_text, LogEvent.fields).where(LogEvent.source_id == source.id)
                if last_id is not None:
                    query = query.where(LogEvent.id > last_id)
                rows = (await session.execute(query.order_by(LogEvent.id).limit(batch_size))).all()
                if not rows:
                    break
                updates = [
                    {"id": row.id, "fields": new}
                    for row in rows
                    if (new := dict(resolve(row.raw_text, source).fields)) != row.fields
                ]
                if updates:
                    await session.execute(update(LogEvent), updates)  # bulk UPDATE by primary key
                changed += len(updates)
                last_id = rows[-1].id
    return changed


def chosen_sources(projects: Sequence[Project], project_id: str | None) -> list[Source]:
    """The files and the push source of the chosen project, or of every valid project."""
    chosen = [find_project(tuple(projects), project_id)] if project_id else [p for p in projects if not p.error]
    return [source for project in chosen for source in project.sources]


async def _main(logs_root: Path, project_id: str | None) -> None:
    from app.core import database  # imported here: it creates the application engine on import

    try:
        for source in chosen_sources(scan_projects(logs_root), project_id):
            print(f"{source.id}: {await backfill(database.SessionLocal, [source])} events updated", flush=True)
    finally:
        await database.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", help="one project (default: every valid project)")
    parser.add_argument("--logs-root", type=Path, help="folder of project folders (default: LOGS_ROOT or ./logs)")
    args = parser.parse_args()
    asyncio.run(_main(args.logs_root or get_settings().logs_root, args.project))
