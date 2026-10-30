import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000, description="Developer question about the logs")
    conversation_id: uuid.UUID | None = Field(default=None, description="Omit to start a new conversation")


class ChatResponse(BaseModel):
    response: str
    status: Literal["COMPLETED", "EXECUTED", "REJECTED"] = "COMPLETED"
    conversation_id: uuid.UUID


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVED", "REJECTED"]


class Memory(BaseModel):
    id: uuid.UUID
    kind: Literal["preference", "observation"]
    content: str
    created_at: datetime
