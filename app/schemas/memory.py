import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

MemoryKind = Literal["preference", "observation"]
MemoryStatus = Literal["active", "conflicted", "superseded", "deleted"]


class MemoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: MemoryKind
    key: str | None
    subject: str | None
    content: str
    attributes: dict[str, Any]
    evidence: str
    status: MemoryStatus
    replaces_id: uuid.UUID | None
    conflicts_with_id: uuid.UUID | None
    source_conversation_id: uuid.UUID | None
    created_at: datetime
