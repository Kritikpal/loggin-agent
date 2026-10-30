"""The index_logs CLI: arguments, exit codes and the interval loop. The pipeline is faked; no database is used."""

from pathlib import Path

import pytest

from app.ingestion.pipeline import IndexReport
from scripts import index_logs

CONFIG = """sources:
  - {id: one, type: file, path: one.log}
  - {id: two, type: file, path: two.log}
"""
FACTORY = object()  # a sentinel: the fakes never touch a database


class FakePipeline:
    def __init__(self, monkeypatch, statuses: dict[str, str] | None = None) -> None:
        self.indexed: list[tuple[str, dict]] = []
        self.reembedded: list[str | None] = []
        self.statuses = statuses or {}
        monkeypatch.setattr(index_logs, "index_source", self.index_source)
        monkeypatch.setattr(index_logs, "reembed_missing", self.reembed_missing)

    async def index_source(self, source, factory, **kwargs) -> IndexReport:
        assert factory is FACTORY
        self.indexed.append((source.id, kwargs))
        status = self.statuses.get(source.id, "ok")
        return IndexReport(source.id, events_read=3, events_stored=3, embedded=3, batches=1, status=status,
                           error="OSError" if status == "error" else None)

    async def reembed_missing(self, factory, source_id=None) -> int:
        self.reembedded.append(source_id)
        return 2


@pytest.fixture
def config(tmp_path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def main(config: Path, *args: str, **kwargs) -> int:
    return index_logs.main(["--config", str(config), *args], session_factory=FACTORY, **kwargs)


@pytest.mark.parametrize("args", [[], ["--source", "one", "--all"], ["--all", "--interval", "0"],
                                  ["--all", "--max-events", "0"], ["--all", "--settle", "-1"]],
                         ids=["neither", "both", "zero_interval", "zero_max_events", "negative_settle"])
def test_bad_arguments_are_argparse_errors(config, args):
    with pytest.raises(SystemExit) as exit_info:
        main(config, *args)

    assert exit_info.value.code == 2


def test_one_source_is_indexed_with_the_given_options(config, monkeypatch, capsys):
    fake = FakePipeline(monkeypatch)

    code = main(config, "--source", "two", "--max-events", "5", "--settle", "0.5")

    assert code == 0 and fake.indexed == [("two", {"max_events": 5, "settle_seconds": 0.5})]
    assert "two: ok - read 3, stored 3, embedded 3, embed failed 0, batches 1" in capsys.readouterr().out


def test_an_unknown_source_is_a_clear_error_and_nothing_is_read(config, monkeypatch, capsys):
    fake = FakePipeline(monkeypatch)

    code = main(config, "--source", "nope")

    assert code == 2 and fake.indexed == [] and "Unknown source 'nope'" in capsys.readouterr().err


def test_a_broken_config_is_a_clear_error(tmp_path, capsys):
    bad = tmp_path / "sources.yaml"
    bad.write_text("sources: nope\n", encoding="utf-8")

    assert main(bad, "--all") == 2 and "sources" in capsys.readouterr().err


def test_all_sources_with_reembed_and_an_error_exit_code(config, monkeypatch, capsys):
    fake = FakePipeline(monkeypatch, {"one": "error"})

    code = main(config, "--all", "--reembed")

    out = capsys.readouterr().out
    assert code == 1 and [i for i, _ in fake.indexed] == ["one", "two"] and fake.reembedded == [None]
    assert "one: error" in out and "error OSError" in out and "re-embedded 2" in out


def test_reembed_for_one_source_targets_it(config, monkeypatch):
    fake = FakePipeline(monkeypatch)

    assert main(config, "--source", "one", "--reembed") == 0 and fake.reembedded == ["one"]


def test_without_a_factory_the_app_database_is_used_and_its_engine_disposed(config, monkeypatch):
    from app.core import database

    disposed = []

    class FakeEngine:
        async def dispose(self) -> None:
            disposed.append(True)

    monkeypatch.setattr(database, "SessionLocal", FACTORY)
    monkeypatch.setattr(database, "engine", FakeEngine())
    fake = FakePipeline(monkeypatch)

    assert index_logs.main(["--config", str(config), "--source", "one"]) == 0
    assert [i for i, _ in fake.indexed] == ["one"] and disposed == [True]


def test_the_interval_loop_repeats_until_ctrl_c_and_exits_cleanly(config, monkeypatch):
    fake = FakePipeline(monkeypatch, {"one": "error"})
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    code = main(config, "--all", "--interval", "30", sleep=fake_sleep)

    assert code == 0 and sleeps == [30.0] * 3 and len(fake.indexed) == 6  # errors do not end the loop
