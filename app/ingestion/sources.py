"""The allowlist of log sources the indexer may read, loaded from sources.yaml.

Nothing outside this file is ever read: source ids come only from here, never from API input. The loader is pure
(it reads the YAML and nothing else) and validates everything up front so a bad config fails at load, not mid-index.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from app.core.errors import AppError, NotFoundError

logger = logging.getLogger(__name__)

SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
PACKAGE_NAME = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")
COMMON_KEYS = {"id", "type", "format", "event_start", "timezone", "app_packages", "field_mappings"}
TYPE_KEYS = {"file": {"path"}}  # local files only


class ConfigError(AppError):
    code = "invalid_sources_config"


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


Source = FileSource


def load_sources(config_path: Path) -> tuple[Source, ...]:
    """Parse and validate the config. A missing or empty file means no sources, which is allowed but logged."""
    if not config_path.is_file():
        logger.warning("No sources config at %s: nothing can be indexed until it exists", config_path)
        return ()
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{config_path.name} is not valid YAML: {exc}") from exc
    if document is None:
        logger.warning("Sources config %s is empty: nothing can be indexed", config_path)
        return ()
    if not isinstance(document, dict) or set(document) != {"sources"} or not isinstance(document["sources"], list):
        raise ConfigError(f"{config_path.name} must be a mapping with a single 'sources:' list")

    sources = tuple(_parse_source(entry, i, config_path.parent) for i, entry in enumerate(document["sources"], 1))
    ids = [source.id for source in sources]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"Duplicate source id: {sorted({i for i in ids if ids.count(i) > 1})}")
    return sources


def get_source(sources: tuple[Source, ...], source_id: str) -> Source:
    for source in sources:
        if source.id == source_id:
            return source
    raise NotFoundError(f"Unknown source '{source_id}': it is not on the allowlist")


def _parse_source(entry: Any, number: int, base_dir: Path) -> Source:
    where = f"sources[{number}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where} must be a mapping")
    source_type = entry.get("type")
    if source_type not in TYPE_KEYS:
        raise ConfigError(f"{where}: type must be one of {sorted(TYPE_KEYS)}, got {source_type!r}")
    unknown = set(entry) - COMMON_KEYS - TYPE_KEYS[source_type]
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(map(str, unknown))}")
    source_id = entry.get("id")
    if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
        raise ConfigError(f"{where}: id must match {SOURCE_ID.pattern}, got {source_id!r}")
    where = f"source '{source_id}'"

    common: dict[str, Any] = {
        "id": source_id,
        "format": _choice(entry, "format", ("text", "json"), "text", where),
        "event_start": _regex(entry.get("event_start"), where),
        "timezone": _timezone(entry.get("timezone", "UTC"), where),
        "app_packages": _packages(entry.get("app_packages", []), where),
        "field_mappings": _mappings(entry.get("field_mappings", {}), where),
    }
    return FileSource(**common, path=_path(entry.get("path"), base_dir, where))


def _choice(entry: dict, key: str, allowed: tuple[str, ...], default: str, where: str) -> str:
    value = entry.get(key, default)
    if value not in allowed:
        raise ConfigError(f"{where}: {key} must be one of {list(allowed)}, got {value!r}")
    return value


def _regex(value: Any, where: str) -> re.Pattern[str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: event_start must be a non-empty regex string")
    try:
        return re.compile(value)
    except re.error as exc:
        raise ConfigError(f"{where}: event_start is not a valid regex: {exc}") from exc


def _timezone(value: Any, where: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ConfigError(f"{where}: unknown timezone {value!r}") from exc
    return value


def _packages(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(p, str) and PACKAGE_NAME.fullmatch(p) for p in value):
        raise ConfigError(f"{where}: app_packages must be a list of Java package names such as com.acme")
    return tuple(value)


def _mappings(value: Any, where: str) -> dict[str, str]:
    valid = isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in value.items())
    if not valid:
        raise ConfigError(f"{where}: field_mappings must map non-empty names to non-empty strings")
    return dict(value)


def _path(value: Any, base_dir: Path, where: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ConfigError(f"{where}: path must be a non-empty string")
    path = Path(value)
    return (path if path.is_absolute() else base_dir / path).resolve()  # relative paths are relative to the config
