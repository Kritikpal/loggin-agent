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
async def test_an_unreadable_file_records_the_reader_error(tmp_path, session_factory):
    path = tmp_path / "utf16.log"
    path.write_bytes(b"\xff\xfe" + "2026-10-29 x\n".encode("utf-16-le"))

    report = await run(file_source(path), session_factory)

    assert (report.status, report.error) == ("error", "UnsupportedEncoding")
    assert (await state(session_factory)).last_error == "UnsupportedEncoding"


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


FORBIDDEN = re.compile(r"\bsubprocess\b|\bos\.system\b|\bos\.popen\b|shell\s*=\s*True|(?<![\w.])eval\(|(?<![\w.])exec\(")


def test_no_ingestion_or_script_code_can_run_commands():
    files = [*(ROOT / "app" / "ingestion").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]
    offenders = [str(f.relative_to(ROOT)) for f in files if FORBIDDEN.search(f.read_text(encoding="utf-8"))]

    assert len(files) > 10 and offenders == []
