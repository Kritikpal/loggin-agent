import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Memory

LIVE = ("active", "conflicted")
MAX_PREFERENCES = 20
MAX_LIST = 100


class MemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def active_preferences(self) -> list[Memory]:
        """Every active preference, by key. They are global settings, so recall never ranks them by similarity."""
        rows = await self.session.scalars(
            select(Memory).where(Memory.kind == "preference", Memory.status == "active")
            .order_by(Memory.key).limit(MAX_PREFERENCES)
        )
        return list(rows)

    async def related_observations(self, terms: str, limit: int = 5) -> list[Memory]:
        """Active observations matching `terms` (websearch syntax: words joined by ' or '), best first, newer on ties.
        websearch_to_tsquery never raises on odd input and drops stop words, so "the and of" matches nothing."""
        query = func.websearch_to_tsquery("english", terms)
        rows = await self.session.scalars(
            select(Memory).where(Memory.kind == "observation", Memory.status == "active", Memory.search.op("@@")(query))
            .order_by(func.ts_rank(Memory.search, query).desc(), Memory.created_at.desc())
            .limit(limit)
        )
        return list(rows)

    async def find_duplicate(self, content_hash: str) -> Memory | None:
        return await self.session.scalar(
            select(Memory).where(Memory.content_hash == content_hash, Memory.status.in_(LIVE)).limit(1)
        )

    async def has_similar(self, content: str, kind: str, floor: float) -> bool:
        """A live memory of the same kind whose trigram similarity to `content` is at least `floor`."""
        match = select(Memory.id).where(
            Memory.kind == kind, Memory.status.in_(LIVE), func.similarity(Memory.content, content) >= floor
        )
        return bool(await self.session.scalar(select(match.exists())))

    async def supersede(self, memory_id: uuid.UUID) -> None:
        """Take a row out of `active`. Must run before inserting its replacement: the partial unique index on
        `key` would reject two active rows. Executes immediately; the caller commits."""
        await self.session.execute(update(Memory).where(Memory.id == memory_id).values(status="superseded"))

    async def list(
        self, kind: str | None = None, statuses: Sequence[str] = LIVE, limit: int = MAX_LIST
    ) -> list[Memory]:
        stmt = select(Memory).where(Memory.status.in_(statuses))
        if kind:
            stmt = stmt.where(Memory.kind == kind)
        return list(await self.session.scalars(stmt.order_by(Memory.created_at.desc()).limit(limit)))

    async def get(self, memory_id: uuid.UUID) -> Memory | None:
        return await self.session.get(Memory, memory_id)

    def add(self, *memories: Memory) -> None:
        self.session.add_all(memories)  # the caller commits, so a turn's decisions land together
