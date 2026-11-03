from pathlib import Path

import pytest

from app.core.errors import NotFoundError
from app.ingestion.sources import FileSource, get_source

ROOT = Path(__file__).resolve().parents[1]


def test_only_listed_sources_can_be_looked_up():
    sources = (FileSource(id="p/a", path=Path("a.log")),)

    assert get_source(sources, "p/a").id == "p/a"
    for unknown in ("nope", "P/A", "../a", "", "a"):
        with pytest.raises(NotFoundError):
            get_source(sources, unknown)


def test_settings_point_at_the_logs_folder_by_default():
    from app.core.config import Settings

    assert Settings(_env_file=None, database_url="postgresql://u@h/db").logs_root == ROOT / "logs"
