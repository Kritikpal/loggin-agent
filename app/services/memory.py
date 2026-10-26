import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import Memory
from app.repositories.memory import LIVE, MemoryRepository
from app.schemas.memory import MemoryRead


class MemoryService:
    """Inspect and correct what the assistant remembers. Nothing is ever hard-deleted: history stays."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.memories = MemoryRepository(session)

    async def list(self, kind: str | None, status: str | None) -> list[MemoryRead]:
        rows = await self.memories.list(kind, (status,) if status else LIVE)
        return [MemoryRead.model_validate(m) for m in rows]

    async def forget(self, memory_id: uuid.UUID) -> None:
        """Soft delete. For a conflicted row this rejects it: the memory it contradicted simply stays active."""
        memory = await self._get(memory_id)
        memory.status = "deleted"
        await self.session.commit()

    async def accept(self, memory_id: uuid.UUID) -> MemoryRead:
        """Settle a flagged conflict in favour of the new row; the row it contradicted is superseded."""
        memory = await self._get(memory_id)
        if memory.status != "conflicted":
            raise ConflictError(f"Memory {memory_id} is {memory.status}, only a conflicted memory can be accepted")
        old = await self.memories.get(memory.conflicts_with_id) if memory.conflicts_with_id else None
        if old is not None and old.status == "active":  # it may have been deleted or replaced since the flag
            await self.memories.supersede(old.id)
        memory.status = "active"
        await self.session.commit()
        return MemoryRead.model_validate(memory)

    async def _get(self, memory_id: uuid.UUID) -> Memory:
        memory = await self.memories.get(memory_id)
        if memory is None:
            raise NotFoundError(f"Memory {memory_id} not found")
        return memory
