"""Shared helpers for project tests: a hand-made Project, a folder-backed project root, and an HTTP client whose
projects, session and embedder are the test ones."""

import shutil
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.ingestion.projects import Project, SkippedFile
from app.ingestion.sources import FileSource, PushSource
from tests.search_helpers import fake_embed_query

FIXTURE_LOGS = Path(__file__).parent / "fixtures" / "projects" / "wso2-gateway"  # the WSO2 test project
SMALL_WSO2_LOG = "wso2carbon-03-02-2026.1.log"  # 97 lines: quick to index
WSO2_YAML = "title: WSO2 API Gateway\ntype: wso2\ntimezone: Asia/Kolkata\n"


def make_project(
    project_id: str = "demo", *, files: Sequence[FileSource] = (), type: str = "wso2", timezone: str = "UTC",
    title: str | None = None, description: str = "", error: str | None = None, skipped: Sequence[SkippedFile] = (),
    push_source: PushSource | None = None,
) -> Project:
    return Project(
        id=project_id, title=title or project_id, description=description, type=type, timezone=timezone,
        folder=Path(project_id), files=tuple(files), skipped=tuple(skipped), error=error,
        push_source=push_source)


def make_project_folder(root: Path, project_id: str, yaml_text: str = WSO2_YAML, *fixtures: str) -> Path:
    """A real project folder under `root` with copies of the named fixture logs."""
    folder = root / project_id
    folder.mkdir(parents=True)
    (folder / "project.yaml").write_text(yaml_text, encoding="utf-8")
    for name in fixtures:
        shutil.copyfile(FIXTURE_LOGS / name, folder / name)
    return folder


@asynccontextmanager
async def projects_client(
    session_factory: Callable, projects: Callable[[], tuple[Project, ...]] | tuple[Project, ...],
    overrides: dict | None = None,
) -> AsyncIterator[AsyncClient]:
    """The real app with get_session / get_projects pointed at the test database and fakes."""
    from app.controllers.dependencies import get_projects
    from app.core.database import get_session
    from app.main import create_app

    async def test_session():
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides.update({
        get_session: test_session,
        get_projects: projects if callable(projects) else (lambda: projects),
        **(overrides or {}),
    })
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()
