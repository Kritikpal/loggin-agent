"""One-off move of the folder projects (LOGS_ROOT/<id>/project.yaml plus log files) into the database.

    python -m scripts.import_projects                 # every valid project under LOGS_ROOT
    python -m scripts.import_projects --logs-root D:/logs --project wso2-gateway

For each project the settings become a `projects` row and every log file is uploaded as events of '<id>/push'.
A project that is already in the database is skipped. If project.yaml names a push_token_env that is set, that
token keeps working; otherwise a new token is printed once. Events the file indexer stored earlier under
'<id>/<file>' are removed, because the same events now arrive under '<id>/push'.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.ingestion.projects import Project, _read, hash_token, new_token, project_from_row, scan_projects
from app.ingestion.store import insert_new_events
from app.ingestion.upload import build_upload_rows
from app.models import LogEvent, SourceIndexState
from app.models.project import ProjectRow

SessionFactory = Callable[[], AsyncSession]
CONFIG_KEYS = ("event_start", "app_packages", "field_mappings")


async def import_project(project: Project, session_factory: SessionFactory) -> str | None:
    """Move one project; returns its new push token, or None when it is already in the database."""
    doc = _read(project.folder / "project.yaml")
    env_name = doc.get("push_token_env")
    token = (os.environ.get(env_name) or str(get_settings().model_extra.get(env_name.lower(), ""))) if env_name else ""
    token = token or new_token()
    row = ProjectRow(
        id=project.id, title=project.title, description=project.description, type=project.type,
        timezone=project.timezone, config={k: doc[k] for k in CONFIG_KEYS if k in doc}, push_token_hash=hash_token(token))
    async with session_factory() as session:
        if await session.get(ProjectRow, project.id) is not None:
            return None
        session.add(row)
        await session.commit()
    source = project_from_row(row).push_source
    for file in project.files:
        rows = await asyncio.to_thread(lambda path=file.path: build_upload_rows(path.read_bytes(), source))
        async with session_factory() as session, session.begin():
            await insert_new_events(session, source.id, rows)
    async with session_factory() as session, session.begin():
        old = [f.id for f in project.files]  # the ids the file indexer used
        await session.execute(delete(LogEvent).where(LogEvent.source_id.in_(old)))
        await session.execute(delete(SourceIndexState).where(SourceIndexState.source_id.in_(old)))
    return token


async def run(root: Path, only: str | None, session_factory: SessionFactory) -> int:
    failed = 0
    for project in scan_projects(root):
        if only and project.id != only:
            continue
        if project.error:
            print(f"skipping {project.id}: {project.error}", file=sys.stderr)
            failed += 1
        elif (token := await import_project(project, session_factory)) is None:
            print(f"{project.id}: already in the database, skipped")
        else:
            print(f"{project.id}: imported {len(project.files)} file(s); push token (shown once): {token}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logs-root", type=Path, help="folder of project folders (default: LOGS_ROOT or ./logs)")
    parser.add_argument("--project", help="import only this project")
    args = parser.parse_args(argv)

    async def go() -> int:
        from app.core import database  # imported here: it creates the application engine on import

        try:
            return await run(args.logs_root or get_settings().logs_root, args.project, database.SessionLocal)
        finally:
            await database.engine.dispose()

    return asyncio.run(go())


if __name__ == "__main__":
    raise SystemExit(main())
