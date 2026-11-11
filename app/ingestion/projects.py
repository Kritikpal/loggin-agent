"""Projects: one folder per application under LOGS_ROOT, described by its project.yaml (spec 2026-11-03).

The logs root is the allowlist: only files found by scanning it are ever read, and a project id from the API is only
looked up among the scanned projects, never joined into a path. A broken project.yaml marks that project invalid and
never hides the others; a file whose name cannot become an id is skipped with a reason the API shows.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from app.core.errors import NotFoundError
from app.ingestion.fields.text_layouts import TYPE_PRESETS
from app.ingestion.sources import (
    ConfigError, FileSource, PushSource, Source, parse_mappings, parse_packages, parse_regex, parse_timezone,
)

logger = logging.getLogger(__name__)

PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")  # also the rule for a file's stem
PROJECT_FILE = "project.yaml"
DEFAULT_INCLUDE = "*.log*"
LOG_EXTENSIONS = (".log", ".txt", ".json")  # dropped from a file name to make its id
MAX_TITLE = 120
MAX_DESCRIPTION = 300  # it is sent with every model call
KEYS = {
    "title", "description", "type", "timezone", "include", "event_start", "app_packages", "field_mappings",
}
PUSH_STEM = "push"  # the file id reserved for the push source
NOT_FOUND = "No such project."


@dataclass(frozen=True)
class SkippedFile:
    name: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class Project:
    id: str
    title: str
    description: str = ""
    type: str = ""
    timezone: str = "UTC"
    include: str = DEFAULT_INCLUDE
    folder: Path | None = None  # None for a database project, which has no files on disk
    files: tuple[FileSource, ...] = ()
    skipped: tuple[SkippedFile, ...] = ()
    error: str | None = None
    push_source: PushSource | None = None

    @property
    def sources(self) -> tuple[Source, ...]:
        """Everything searchable in this project: its files, then the push source if it has one."""
        return (*self.files, *([self.push_source] if self.push_source else []))

    @property
    def status(self) -> Literal["ok", "invalid"]:
        return "invalid" if self.error else "ok"


def scan_projects(root: Path) -> tuple[Project, ...]:
    """Every project folder directly under `root`, sorted by id. A missing root means no projects."""
    if not root.is_dir():
        logger.warning("No logs root at %s: there are no projects until it exists", root)
        return ()
    projects = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not (folder / PROJECT_FILE).is_file():
            continue
        if not PROJECT_ID.fullmatch(folder.name):
            logger.warning("Skipping project folder %r: the name must match %s", folder.name, PROJECT_ID.pattern)
            continue
        projects.append(_load(folder))
    return tuple(projects)


def find_project(projects: tuple[Project, ...], project_id: str) -> Project:
    """The same 404 message for an unknown and a malformed id, so the reply never echoes or explains the input."""
    for project in projects:
        if project.id == project_id:
            return project
    raise NotFoundError(NOT_FOUND)


def _load(folder: Path) -> Project:
    """Any failure becomes this project's error: one bad or unreadable project.yaml must never hide the others."""
    try:
        return _parse(folder, _read(folder / PROJECT_FILE))
    except ConfigError as exc:
        error = exc.message
    except (OSError, ValueError, TypeError) as exc:  # unreadable file, a bad glob, a value of an unexpected type
        error = f"{PROJECT_FILE} could not be loaded ({type(exc).__name__}): {exc}"
    logger.warning("Project %s is invalid: %s", folder.name, error)
    return Project(id=folder.name, title=folder.name, folder=folder, error=error)


def _read(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{PROJECT_FILE} is not valid UTF-8 YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{PROJECT_FILE} must be a mapping of settings")
    unknown = set(map(str, document)) - KEYS
    if unknown:
        raise ConfigError(f"{PROJECT_FILE}: unknown keys {sorted(unknown)}")
    return document


def project_from_config(project_id: str, doc: dict[str, Any]) -> Project:
    """A database project: validated like a project.yaml, with its pushed/uploaded logs as the only source."""
    where = f"project '{project_id}'"
    title, description, include, settings = _common(doc, where)
    push = PushSource(id=f"{project_id}/{PUSH_STEM}", **settings)
    return Project(
        id=project_id, title=title, description=description, type=doc["type"], timezone=settings["timezone"],
        include=include, push_source=push)


def row_doc(row: Any) -> dict[str, Any]:
    """A database row as the same mapping a project.yaml gives."""
    return {"title": row.title, "description": row.description, "type": row.type, "timezone": row.timezone,
            **row.config}


def project_from_row(row: Any) -> Project:
    """Any failure becomes this project's error, as for a folder: one bad row must never hide the others."""
    try:
        return project_from_config(row.id, row_doc(row))
    except ConfigError as exc:
        error = exc.message
    except (ValueError, TypeError) as exc:
        error = f"the project's settings could not be loaded ({type(exc).__name__}): {exc}"
    logger.warning("Project %s is invalid: %s", row.id, error)
    return Project(id=row.id, title=row.title, error=error)


def _parse(folder: Path, doc: dict[str, Any]) -> Project:
    where = f"project '{folder.name}'"
    title, description, include, settings = _common(doc, where)
    files, skipped = _files(folder, include, settings)
    return Project(
        id=folder.name, title=title, description=description, type=doc["type"], timezone=settings["timezone"],
        include=include, folder=folder, files=files, skipped=skipped)


def _common(doc: dict[str, Any], where: str) -> tuple[str, str, str, dict[str, Any]]:
    """The settings both kinds of project share: title, description, include and the parsing settings."""
    title = _text(doc.get("title"), "title", MAX_TITLE, where, required=True)
    description = _text(doc.get("description", ""), "description", MAX_DESCRIPTION, where, required=False)
    type_name = doc.get("type")
    preset = TYPE_PRESETS.get(type_name) if isinstance(type_name, str) else None  # a list is not hashable
    if preset is None:
        raise ConfigError(f"{where}: type must be one of {sorted(TYPE_PRESETS)}, got {doc.get('type')!r}")
    include = doc.get("include", DEFAULT_INCLUDE)
    if not isinstance(include, str) or not include.strip() or "/" in include or "\\" in include or ".." in include:
        raise ConfigError(f"{where}: include must be a file-name glob such as *.log, got {include!r}")
    settings = {
        "format": preset.format,
        "event_start": parse_regex(doc.get("event_start", preset.event_start), where),
        "timezone": parse_timezone(doc.get("timezone", "UTC"), where),
        "app_packages": parse_packages(doc.get("app_packages", list(preset.app_packages)), where),
        "field_mappings": parse_mappings(doc.get("field_mappings", {}), where),
    }
    return title, description, include, settings


def file_stem(name: str) -> str:
    """A file's id within its project: the lower-cased name minus one known log extension. Only a known extension is
    dropped, so rotated files (app.log.1, app.log.2) keep distinct ids instead of colliding on their last suffix."""
    lowered = name.lower()
    for extension in LOG_EXTENSIONS:
        if lowered.endswith(extension) and len(lowered) > len(extension):
            return lowered[: -len(extension)]
    return lowered


def _text(value: Any, key: str, limit: int, where: str, *, required: bool) -> str:
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        need = "a non-empty string" if required else "a string"
        raise ConfigError(f"{where}: {key} must be {need} of at most {limit} characters")
    return value.strip()


def _files(
    folder: Path, include: str, settings: dict[str, Any],
) -> tuple[tuple[FileSource, ...], tuple[SkippedFile, ...]]:
    """The project's log files: regular, non-hidden files matching `include`, directly in the folder. Never recursive."""
    real_folder = folder.resolve()
    candidates = sorted(
        p for p in folder.glob(include)
        if p.name != PROJECT_FILE and not p.name.startswith(".") and p.parent == folder and p.is_file())
    skipped, named = [], []
    for path in candidates:
        stem = file_stem(path.name)
        if not path.resolve().is_relative_to(real_folder):
            skipped.append(SkippedFile(path.name, "it links to a file outside the project folder"))
        elif not PROJECT_ID.fullmatch(stem):
            skipped.append(SkippedFile(path.name, f"the file name (without extension) must match {PROJECT_ID.pattern}"))
        else:
            named.append((stem, path))
    clashes = Counter(stem for stem, _ in named)
    files = []
    for stem, path in named:
        if clashes[stem] > 1:
            skipped.append(SkippedFile(path.name, f"another file gives the same id '{stem}'"))
        else:
            files.append(FileSource(id=f"{folder.name}/{stem}", path=path.resolve(), **settings))
    return tuple(files), tuple(skipped)
