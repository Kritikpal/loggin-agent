import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # everything but tab, newline and carriage return


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000, description="Developer question about the logs")
    conversation_id: uuid.UUID | None = Field(default=None, description="Omit to start a new conversation")

    @field_validator("message")
    @classmethod
    def _clean_message(cls, message: str) -> str:
        cleaned = CONTROL_CHARS.sub("", message).strip()
        if not cleaned:
            raise ValueError("message must contain text")
        return cleaned


class ToolCall(BaseModel):
    """One tool call the agent made, shown to the developer so they can check the searches behind an answer."""

    order: int
    name: str
    args: dict[str, Any]
    status: Literal["success", "error"]
    result: str | None


class ChatResponse(BaseModel):
    response: str
    status: Literal["COMPLETED", "EXECUTED", "REJECTED"] = "COMPLETED"
    conversation_id: uuid.UUID
    tool_calls: list[ToolCall] = Field(default_factory=list)
    memory_conflicts: list[dict[str, Any]] = Field(default_factory=list)  # read by the chat UI; always empty for now


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVED", "REJECTED"]


class Memory(BaseModel):
    id: uuid.UUID
    kind: Literal["preference", "observation"]
    content: str
    created_at: datetime
