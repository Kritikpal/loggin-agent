"""The index_logs CLI: arguments, exit codes and the interval loop. The pipeline is faked; no database is used."""

from pathlib import Path

import pytest

from app.ingestion.pipeline import IndexReport
from scripts import index_logs

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


def project(root: Path, name: str, yaml_text: str, *files: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "project.yaml").write_text(yaml_text, encoding="utf-8")
    for file in files:
        (root / name / file).write_text("x\n", encoding="utf-8")


@pytest.fixture
def root(tmp_path) -> Path:
    project(tmp_path, "one", "title: One\ntype: wso2\n", "a.log", "b.log")
    project(tmp_path, "two", "title: Two\ntype: logback\n", "c.log")
    return tmp_path


def main(root: Path, *args: str, **kwargs) -> int:
    return index_logs.main(["--logs-root", str(root), *args], session_factory=FACTORY, **kwargs)


@pytest.mark.parametrize("args", [[], ["--project", "one", "--all"], ["--all", "--interval", "0"],
                                  ["--all", "--max-events", "0"], ["--all", "--settle", "-1"],
                                  ["--source", "one"], ["--config", "x.yaml", "--all"]],
                         ids=["neither", "both", "zero_interval", "zero_max_events", "negative_settle",
                              "old_source_flag", "old_config_flag"])
def test_bad_arguments_are_argparse_errors(root, args):
    with pytest.raises(SystemExit) as exit_info:
        main(root, *args)

    assert exit_info.value.code == 2


def test_project_flag_indexes_only_that_project(root, monkeypatch, capsys):
    fake = FakePipeline(monkeypatch)

    code = main(root, "--project", "one", "--max-events", "5", "--settle", "0.5")

    assert code == 0
    assert fake.indexed == [("one/a", {"max_events": 5, "settle_seconds": 0.5}),
                            ("one/b", {"max_events": 5, "settle_seconds": 0.5})]
    assert "one/a: ok - read 3, stored 3, embedded 3, embed failed 0, batches 1" in capsys.readouterr().out


def test_file_flag_indexes_one_file(root, monkeypatch):
    fake = FakePipeline(monkeypatch)

    assert main(root, "--file", "one/b") == 0 and [i for i, _ in fake.indexed] == ["one/b"]


@pytest.mark.parametrize("args, message", [(["--project", "nope"], "No such project."),
                                           (["--file", "one/zzz"], "Unknown file 'one/zzz'"),
                                           (["--file", "nope/a"], "No such project."),
                                           (["--file", "noslash"], "Unknown file 'noslash'")])
def test_unknown_project_or_file_is_a_usage_error(root, monkeypatch, capsys, args, message):
    fake = FakePipeline(monkeypatch)

    code = main(root, *args)

    assert code == 2 and fake.indexed == [] and message in capsys.readouterr().err


def test_an_invalid_project_is_a_usage_error_with_its_reason(root, monkeypatch, capsys):
    project(root, "broken", "type: nope\n", "x.log")
    fake = FakePipeline(monkeypatch)

    assert main(root, "--project", "broken") == 2 and fake.indexed == []
    assert "broken" in capsys.readouterr().err


def test_all_skips_invalid_projects_and_says_so(root, monkeypatch, capsys):
    project(root, "broken", "type: nope\n", "x.log")
    fake = FakePipeline(monkeypatch)

    code = main(root, "--all")

    captured = capsys.readouterr()
    assert code == 0 and [i for i, _ in fake.indexed] == ["one/a", "one/b", "two/c"]
    assert "skipping project broken" in captured.err


def test_all_with_reembed_and_an_error_exit_code(root, monkeypatch, capsys):
    fake = FakePipeline(monkeypatch, {"one/a": "error"})

    code = main(root, "--all", "--reembed")

    out = capsys.readouterr().out
    assert code == 1 and len(fake.indexed) == 3 and fake.reembedded == [None]
    assert "one/a: error" in out and "error OSError" in out and "re-embedded 2" in out


def test_reembed_for_a_project_targets_its_files(root, monkeypatch):
    fake = FakePipeline(monkeypatch)

    assert main(root, "--project", "one", "--reembed") == 0 and fake.reembedded == ["one/a", "one/b"]


def test_logs_root_defaults_to_the_setting(root, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "logs_root", root)
    fake = FakePipeline(monkeypatch)

    assert index_logs.main(["--project", "two"], session_factory=FACTORY) == 0
    assert [i for i, _ in fake.indexed] == ["two/c"]


def test_without_a_factory_the_app_database_is_used_and_its_engine_disposed(root, monkeypatch):
    from app.core import database

    disposed = []

    class FakeEngine:
        async def dispose(self) -> None:
            disposed.append(True)

    monkeypatch.setattr(database, "SessionLocal", FACTORY)
    monkeypatch.setattr(database, "engine", FakeEngine())
    fake = FakePipeline(monkeypatch)

    assert index_logs.main(["--logs-root", str(root), "--file", "one/a"]) == 0
    assert [i for i, _ in fake.indexed] == ["one/a"] and disposed == [True]


def test_the_interval_loop_repeats_until_ctrl_c_and_exits_cleanly(root, monkeypatch):
    fake = FakePipeline(monkeypatch, {"one/a": "error"})
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    code = main(root, "--all", "--interval", "30", sleep=fake_sleep)

    assert code == 0 and sleeps == [30.0] * 3 and len(fake.indexed) == 9  # errors do not end the loop
