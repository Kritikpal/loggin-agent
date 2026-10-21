from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import text


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: text(2000) = Field(description="Natural-language question about sales")


class ToolCallRecord(BaseModel):
    """What the agent actually ran. `result` is the raw tool output the answer was based on."""

    order: int = Field(description="1-based position in the run's execution order")
    name: str
    args: dict[str, Any]
    status: str = Field(description="'success' or 'error'")
    result: str | None = None


class ChatResponse(BaseModel):
    response: str
    tool_calls: list[ToolCallRecord]
