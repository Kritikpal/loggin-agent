"""5.8: the indexing pipeline end to end against the test database, always with a fake embedder."""

import hashlib
import re
import shutil
import threading
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from app.core.errors import NotFoundError
from app.ingestion import pipeline
from app.ingestion.pipeline import IndexReport, index_all, index_source, reembed_missing
from app.ingestion.readers.base import Cursor
from app.ingestion.sources import FileSource, get_source
from app.ingestion.store import load_cursor
from app.models import LogEvent, SourceIndexState
from tests.test_file_reader import ts
from tests.test_semantic_text import FIXTURES

ROOT = Path(__file__).resolve().parents[1]


class FakeEmbedder:
    """Deterministic vectors from a hash of each text; records every call and the thread it ran on."""

    def __init__(self, fail: bool = False, dims: int = EMBEDDING_DIMENSIONS, drop: int = 0) -> None:
        self.fail, self.dims, self.drop = fail, dims, drop
        self.calls: list[list[str]] = []
        self.threads: set[int] = set()

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        self.threads.add(threading.get_ident())
        if self.fail:
            raise RuntimeError("model failed on password=hunter2")  # the message must never be stored or logged
        vectors = [[b / 255 for b in hashlib.sha256(t.encode()).digest()] * 12 for t in texts]
        return [v[:self.dims] for v in vectors][self.drop:]

    @property
    def texts(self) -> list[str]:
        return [t for call in self.calls for t in call]


@pytest.fixture
def log(tmp_path) -> Path:
    path = tmp_path / "app.log"
    shutil.copy(FIXTURES / "spring_boot.log", path)
    return path


def file_source(path: Path, **kwargs) -> FileSource:
    return FileSource(id="demo", path=path, app_packages=("com.example",), **kwargs)


async def run(source, factory, embed=None, **kwargs) -> IndexReport:
    return await index_source(source, factory, embed=embed or FakeEmbedder(), settle_seconds=0.0, **kwargs)


async def rows(factory, source_id: str = "demo") -> list[LogEvent]:
    async with factory() as s:
        query = select(LogEvent).where(LogEvent.source_id == source_id).order_by(LogEvent.occurred_at, LogEvent.event_key)
        return list((await s.execute(query)).scalars())


async def cursor_of(factory, source_id: str = "demo") -> Cursor:
    async with factory() as s:
        return await load_cursor(s, source_id)


async def state(factory, source_id: str = "demo") -> SourceIndexState | None:
    async with factory() as s:
        return await s.get(SourceIndexState, source_id)


@pytest.mark.integration
async def test_the_spring_fixture_is_indexed_with_evidence(log, session_factory):
    report = await run(file_source(log), session_factory)

    stored = await rows(session_factory)
    npe = next(r for r in stored if "NullPointerException" in r.raw_text and r.raw_text.count("\n") > 5)
    data = log.read_bytes()
    original = data[npe.locator["offset_start"]:npe.locator["offset_end"]]
    assert (report.events_read, report.events_stored, report.embedded, report.status) == (15, 15, 15, "ok")
    assert len(stored) == 15
    assert "\n\n" in npe.raw_text and "\tat com.example.demo.service.OrderService" in npe.raw_text
    assert npe.level == "ERROR" and npe.occurred_at is not None and {"pid", "thread"} <= set(npe.fields)
    assert "OrderService.processCheckout" in npe.semantic_text and "org.springframework" not in npe.semantic_text
    assert hashlib.sha256(original).hexdigest() == npe.locator["sha256"]
    assert original.decode().strip("\r\n") == npe.raw_text
    assert len(npe.embedding) == EMBEDDING_DIMENSIONS and npe.embedding_model == EMBEDDING_MODEL
    final = await state(session_factory)
    assert final.last_error is None and final.indexed_through == max(r.occurred_at for r in stored)


@pytest.mark.integration
async def test_running_twice_changes_nothing(log, session_factory):
    await run(file_source(log), session_factory)
    first = await cursor_of(session_factory)
    embed = FakeEmbedder()
    again = await run(file_source(log), session_factory, embed)

    assert len(await rows(session_factory)) == 15 and await cursor_of(session_factory) == first
    assert again.events_read == 0 and embed.calls == []


@pytest.mark.integration
async def test_appended_events_alone_are_added(log, session_factory):
    await run(file_source(log), session_factory)
    before = await cursor_of(session_factory)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(ts(1, "ERROR 1 --- [main] c.e.X : later one") + ts(2, "INFO 1 --- [main] c.e.X : later two"))
    embed = FakeEmbedder()
    report = await run(file_source(log), session_factory, embed)

    assert report.events_read == 2 and len(embed.texts) == 2 and len(await rows(session_factory)) == 17
    assert (await cursor_of(session_factory)).offset == log.stat().st_size > before.offset


SECRETS = ("hunter2", "abc.def.ghi", "alice@example.com", "4111 1111 1111 1111", "4111111111111111")


@pytest.mark.integration
async def test_planted_secrets_reach_neither_the_database_nor_the_embedder(tmp_path, session_factory):
    path = tmp_path / "secret.log"
    path.write_text(
        ts(0, "ERROR 1 --- [main] c.e.Login : login failed password=hunter2 for alice@example.com")
        + ts(1, "WARN 1 --- [main] c.e.Pay : call with Authorization: Bearer abc.def.ghi card 4111 1111 1111 1111"),
        encoding="utf-8",
    )
    embed = FakeEmbedder()
    await run(file_source(path), session_factory, embed)

    stored = await rows(session_factory)
    async with session_factory() as s:
        state_text = str((await s.execute(select(SourceIndexState.__table__))).all())
    columns = [f"{r.event_key}{r.raw_text}{r.fields}{r.semantic_text}{r.locator}" for r in stored]
    haystack = "\n".join(columns + embed.texts + [state_text])
    assert len(stored) == 2 and len(embed.texts) == 2
    assert [secret for secret in SECRETS if secret in haystack] == []


@pytest.mark.integration
async def test_an_embedding_failure_stores_the_rows_and_a_backfill_fills_them(log, session_factory, caplog):
    report = await run(file_source(log), session_factory, FakeEmbedder(fail=True))

    stored = await rows(session_factory)
    assert (report.embedded, report.embed_failed, report.status) == (0, 15, "ok")
    assert all(r.embedding is None for r in stored) and len(stored) == 15
    assert (await cursor_of(session_factory)).offset == log.stat().st_size
    assert "RuntimeError" in caplog.text and "hunter2" not in caplog.text

    backfill = FakeEmbedder()
    assert await reembed_missing(session_factory, embed=backfill, chunk=4) == 15
    assert len(backfill.calls) == 4 and await reembed_missing(session_factory, embed=backfill) == 0
    filled = await rows(session_factory)
    assert all(len(r.embedding) == EMBEDDING_DIMENSIONS and r.embedding_model == EMBEDDING_MODEL for r in filled)


@pytest.mark.integration
@pytest.mark.parametrize("embed", [FakeEmbedder(dims=3), FakeEmbedder(drop=1)], ids=["wrong_dims", "too_few"])
async def test_wrong_vectors_count_as_an_embedding_failure(log, session_factory, embed):
    report = await run(file_source(log), session_factory, embed)

    assert (report.embedded, report.embed_failed) == (0, 15)
    assert all(r.embedding is None for r in await rows(session_factory))


@pytest.mark.integration
async def test_non_finite_vectors_count_as_an_embedding_failure(log, session_factory):
    report = await run(file_source(log), session_factory, lambda texts: [[float("nan")] * EMBEDDING_DIMENSIONS for _ in texts])

    assert (report.embedded, report.embed_failed, report.status) == (0, 15, "ok")


@pytest.mark.integration
async def test_reembed_stops_at_the_first_failing_chunk_and_can_target_one_source(log, session_factory):
    await run(file_source(log), session_factory, FakeEmbedder(fail=True))
    await run(FileSource(id="other", path=log), session_factory, FakeEmbedder(fail=True))
    failing = FakeEmbedder(fail=True)

    assert await reembed_missing(session_factory, embed=failing, chunk=4) == 0 and len(failing.calls) == 1
    assert await reembed_missing(session_factory, "other", embed=FakeEmbedder()) == 15
    assert all(r.embedding is None for r in await rows(session_factory))


@pytest.mark.integration
async def test_a_missing_file_records_the_error_and_keeps_everything(log, session_factory):
    await run(file_source(log), session_factory)
    before = await cursor_of(session_factory)
    log.unlink()

    report = await run(file_source(log), session_factory)

    assert (report.status, report.error) == ("missing", "FileNotFoundError")
    assert (await state(session_factory)).last_error == "FileNotFoundError"
    assert len(await rows(session_factory)) == 15 and await cursor_of(session_factory) == before

    shutil.copy(FIXTURES / "spring_boot.log", log)  # back as a new file: re-read, same bytes give the same keys
    assert (await run(file_source(log), session_factory)).status == "rotated"
    assert (await state(session_factory)).last_error is None and len(await rows(session_factory)) == 15


@pytest.mark.integration
async def test_an_unreadable_file_records_the_reader_error(tmp_path, session_factory):
    path = tmp_path / "utf16.log"
    path.write_bytes(b"\xff\xfe" + "2026-10-29 x\n".encode("utf-16-le"))

    report = await run(file_source(path), session_factory)

    assert (report.status, report.error) == ("error", "UnsupportedEncoding")
    assert (await state(session_factory)).last_error == "UnsupportedEncoding"


@pytest.mark.integration
async def test_a_rotated_file_adds_rows_and_keeps_the_old_ones(log, session_factory):
    await run(file_source(log), session_factory)
    log.write_text(ts(1, "ERROR 1 --- [main] c.e.X : after rotation") + ts(2, "INFO 1 --- [main] c.e.X : more"), encoding="utf-8")

    report = await run(file_source(log), session_factory)

    assert (report.status, report.events_stored) == ("rotated", 2)
    assert len(await rows(session_factory)) == 17


@pytest.mark.integration
async def test_a_json_source_with_nul_escapes_and_ecs_records_is_stored_clean(tmp_path, session_factory):
    path = tmp_path / "app.json.log"
    path.write_text(
        '{"@timestamp":"2026-10-29T10:00:00Z","log.level":"ERROR","message":"boom \\u0000 here",'
        '"service.name":"pay","error.type":"java.lang.IllegalStateException","error.message":"bad\\u0000"}\n'
        '{"timestamp":"2026-10-29T10:00:01Z","level":"INFO","msg":"ok\\u0000","user":{"id":7}}\n',
        encoding="utf-8",
    )
    report = await run(FileSource(id="demo", path=path, format="json"), session_factory)

    stored = await rows(session_factory)
    assert (report.status, len(stored)) == ("ok", 2)
    assert [r.level for r in stored] == ["ERROR", "INFO"] and all(r.occurred_at for r in stored)
    assert not any("\x00" in f"{r.raw_text}{r.semantic_text}{r.fields}" for r in stored)


def test_an_unknown_source_id_is_refused_before_anything_is_read(log):
    with pytest.raises(NotFoundError):
        get_source((file_source(log),), "nope")


@pytest.mark.integration
async def test_blocking_work_runs_off_the_event_loop_thread(log, session_factory, monkeypatch):
    read_threads: set[int] = set()
    original = pipeline.FileReader.read_batch

    def spy(self, *args):
        read_threads.add(threading.get_ident())
        return original(self, *args)

    monkeypatch.setattr(pipeline.FileReader, "read_batch", spy)
    embed = FakeEmbedder()
    await run(file_source(log), session_factory, embed)

    loop_thread = threading.get_ident()
    assert embed.threads and loop_thread not in embed.threads
    assert read_threads and loop_thread not in read_threads


@pytest.mark.integration
async def test_small_batches_give_the_same_rows(log, session_factory, tmp_path):
    other = tmp_path / "copy.log"
    shutil.copy(log, other)
    whole = await run(file_source(log), session_factory)
    small = await run(FileSource(id="small", path=other, app_packages=("com.example",)), session_factory, max_events=3, embed_batch_size=2)

    project = lambda stored: [(r.locator["offset_start"], r.raw_text, r.semantic_text, r.level) for r in stored]  # noqa: E731
    assert project(await rows(session_factory)) == project(await rows(session_factory, "small"))
    assert (whole.batches, small.batches >= 5, small.events_stored) == (1, True, 15)


@pytest.mark.integration
async def test_an_event_that_breaks_the_parser_is_stored_raw(log, session_factory, monkeypatch):
    def broken(text, source):
        raise ValueError("parser bug")

    monkeypatch.setattr(pipeline, "resolve", broken)
    report = await run(file_source(log), session_factory)

    stored = await rows(session_factory)
    assert report.events_stored == 15 and all(r.level is None and r.fields == {} for r in stored)
    assert all(r.semantic_text and r.raw_text.startswith(r.semantic_text[:20]) for r in stored)


@pytest.mark.integration
async def test_an_unexpected_failure_is_recorded_by_class_name_and_others_continue(log, session_factory, tmp_path, monkeypatch, caplog):
    good = tmp_path / "good.log"
    shutil.copy(log, good)
    original = pipeline.upsert_events

    async def flaky(session, source_id, rows_):
        if source_id == "demo":
            raise ConnectionError("db down, last line was password=hunter2")
        await original(session, source_id, rows_)

    monkeypatch.setattr(pipeline, "upsert_events", flaky)
    reports = await index_all(
        [file_source(log), FileSource(id="gone", path=tmp_path / "gone.log"), FileSource(id="good", path=good)],
        session_factory, embed=FakeEmbedder(), settle_seconds=0.0,
    )

    assert [(r.source_id, r.status, r.error) for r in reports] == [
        ("demo", "error", "ConnectionError"), ("gone", "missing", "FileNotFoundError"), ("good", "ok", None)]
    assert (await state(session_factory)).last_error == "ConnectionError" and await rows(session_factory) == []
    assert await cursor_of(session_factory) == Cursor() and len(await rows(session_factory, "good")) == 15
    assert "hunter2" not in caplog.text


async def test_a_database_that_is_down_still_gives_an_error_report(log, caplog):
    def no_database():
        raise OSError("connection refused")

    report = await run(file_source(log), no_database)

    assert (report.status, report.error, report.events_read) == ("error", "OSError", 0)
    assert "could not record" in caplog.text.lower()


FORBIDDEN = re.compile(r"\bsubprocess\b|\bos\.system\b|\bos\.popen\b|shell\s*=\s*True|(?<![\w.])eval\(|(?<![\w.])exec\(")


def test_no_ingestion_or_script_code_can_run_commands():
    files = [*(ROOT / "app" / "ingestion").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]
    offenders = [str(f.relative_to(ROOT)) for f in files if FORBIDDEN.search(f.read_text(encoding="utf-8"))]

    assert len(files) > 10 and offenders == []
