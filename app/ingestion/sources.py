"""A log source: one file the indexer may read, with the parsing settings of the project it belongs to.

Sources come only from scanning the project folders (app.ingestion.projects), never from API input. The validators
here check project.yaml values up front, so a bad setting fails at scan time, not mid-index.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.errors import AppError, NotFoundError

PACKAGE_NAME = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")


class ConfigError(AppError):
    code = "invalid_project_config"


@dataclass(frozen=True, kw_only=True)
class SourceBase:
    id: str
    format: Literal["text", "json"] = "text"
    event_start: re.Pattern[str] | None = None  # a line matching this starts a new event; None = the default preset
    timezone: str = "UTC"  # for timestamps that carry no zone of their own
    app_packages: tuple[str, ...] = ()  # the source's own Java packages, for build_semantic_text
    field_mappings: Mapping[str, str] = field(default_factory=dict)  # canonical field name -> key in this source's logs


@dataclass(frozen=True, kw_only=True)
class FileSource(SourceBase):
    path: Path


@dataclass(frozen=True, kw_only=True)
class PushSource(SourceBase):
    """Logs a project's applications push to the ingest endpoint. `token_env` names the environment variable that
    holds the bearer token, so the secret itself is never kept on the project."""

    token_env: str


Source = FileSource | PushSource


def get_source(sources: tuple[Source, ...], source_id: str) -> Source:
    for source in sources:
        if source.id == source_id:
            return source
    raise NotFoundError(f"Unknown source '{source_id}': it is not on the allowlist")


def parse_regex(value: Any, where: str) -> re.Pattern[str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: event_start must be a non-empty regex string")
    try:
        return re.compile(value)
    except re.error as exc:
        raise ConfigError(f"{where}: event_start is not a valid regex: {exc}") from exc


def parse_timezone(value: Any, where: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ConfigError(f"{where}: unknown timezone {value!r}") from exc
    return value


def parse_packages(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(p, str) and PACKAGE_NAME.fullmatch(p) for p in value):
        raise ConfigError(f"{where}: app_packages must be a list of Java package names such as com.acme")
    return tuple(value)


def parse_mappings(value: Any, where: str) -> dict[str, str]:
    valid = isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in value.items())
    if not valid:
        raise ConfigError(f"{where}: field_mappings must map non-empty names to non-empty strings")
    return dict(value)
