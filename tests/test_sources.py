import logging
from pathlib import Path

import pytest

from app.core.errors import NotFoundError
from app.ingestion.sources import ConfigError, FileSource, get_source, load_sources

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "sources.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def test_a_file_source_loads_with_its_options(tmp_path):
    config = write(tmp_path, r"""
sources:
  - id: api.v2
    type: file
    path: logs/app.log
    format: json
    event_start: '^\d{4}-'
    timezone: Asia/Kolkata
    app_packages: [com.acme, org.springframework.myapp]
    field_mappings: {message: msg}
""")

    (file_source,) = load_sources(config)

    assert isinstance(file_source, FileSource) and file_source.format == "json"
    assert file_source.path == (tmp_path / "logs/app.log").resolve()  # relative to the config file
    assert file_source.event_start.match("2026-10-29") and file_source.timezone == "Asia/Kolkata"
    assert file_source.field_mappings == {"message": "msg"}


def test_an_absolute_path_is_kept(tmp_path):
    target = tmp_path / "elsewhere" / "a.log"

    (source,) = load_sources(write(tmp_path, f"sources:\n  - id: a\n    type: file\n    path: '{target.as_posix()}'\n"))

    assert source.path == target.resolve()


@pytest.mark.parametrize("missing", [False, True])
def test_a_missing_or_empty_config_means_no_sources_and_says_so(tmp_path, caplog, missing):
    config = tmp_path / "sources.yaml" if missing else write(tmp_path, "")

    with caplog.at_level(logging.WARNING):
        assert load_sources(config) == ()

    assert "nothing can be indexed" in caplog.text


def test_an_empty_sources_list_is_valid(tmp_path):
    assert load_sources(write(tmp_path, "sources: []\n")) == ()


@pytest.mark.parametrize("text, message", [
    ("sources:\n  - id: a\n    type: ftp\n    path: x\n", "type must be one of"),
    ("sources:\n  - id: A_b\n    type: file\n    path: x\n", "id must match"),
    ("sources:\n  - type: file\n    path: x\n", "id must match"),
    ("sources:\n  - id: a\n    type: file\n", "path must be a non-empty string"),
    ("sources:\n  - id: a\n    type: file\n    path: x\n    container: c\n", "unknown keys"),
    ("sources:\n  - id: a\n    type: file\n    path: x\n    glob: '*.log'\n", "unknown keys"),
    ("sources:\n  - id: a\n    type: docker\n    container: c\n", r"type must be one of \['file'\], got 'docker'"),  # Docker support was dropped
    ("sources:\n  - id: a\n    type: file\n    path: x\n    format: xml\n", "format must be one of"),
    ("sources:\n  - id: a\n    type: file\n    path: x\n    event_start: '('\n", "not a valid regex"),
    ("sources:\n  - id: a\n    type: file\n    path: x\n    event_start: 5\n", "non-empty regex string"),
    ("sources:\n  - id: a\n    type: file\n    path: x\n    timezone: Mars/Base\n", "unknown timezone"),
    ("sources:\n  - id: a\n    type: file\n    path: x\n    app_packages: ['com.acme; drop']\n", "app_packages"),
    ("sources:\n  - id: a\n    type: file\n    path: x\n    app_packages: com.acme\n", "app_packages"),
    ("sources:\n  - id: a\n    type: file\n    path: x\n    field_mappings: {message: 5}\n", "field_mappings"),
    ("sources:\n  - id: a\n    type: file\n    path: x\n  - id: a\n    type: file\n    path: y\n", "Duplicate source id"),
    ("sources: nope\n", "single 'sources:' list"),
    ("other: 1\n", "single 'sources:' list"),
    ("- a\n- b\n", "single 'sources:' list"),
    ("sources:\n  - just-a-string\n", "must be a mapping"),
    ("sources: [unclosed\n", "not valid YAML"),
])
def test_invalid_configs_fail_at_load_with_a_clear_error(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_sources(write(tmp_path, text))


def test_yaml_cannot_construct_python_objects(tmp_path):
    payload = "sources:\n  - id: a\n    type: file\n    path: !!python/object/apply:os.getcwd []\n"

    with pytest.raises(ConfigError, match="not valid YAML"):
        load_sources(write(tmp_path, payload))


def test_only_listed_sources_can_be_looked_up(tmp_path):
    sources = load_sources(write(tmp_path, "sources:\n  - id: a\n    type: file\n    path: x\n"))

    assert get_source(sources, "a").id == "a"
    for unknown in ("nope", "A", "../a", ""):
        with pytest.raises(NotFoundError):
            get_source(sources, unknown)


def test_settings_point_at_sources_yaml_by_default():
    from app.core.config import get_settings

    assert get_settings().sources_config_path == ROOT / "sources.yaml"
