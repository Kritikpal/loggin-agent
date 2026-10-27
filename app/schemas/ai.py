import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.campaign import CampaignRead
from app.schemas.common import text

WorkflowStatus = Literal["COMPLETED", "WAITING_APPROVAL", "EXECUTED", "REJECTED", "INVALID_ACTION", "BLOCKED"]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: text(2000) = Field(description="Natural-language question about sales")
    conversation_id: uuid.UUID | None = Field(
        default=None, description="Continue this conversation; omit to start a new one (returned in the response)"
    )


class ToolCallRecord(BaseModel):
    """What the agent actually ran. `result` is the raw tool output the answer was based on."""

    order: int = Field(description="1-based position in the run's execution order")
    name: str
    args: dict[str, Any]
    status: str = Field(description="'success' or 'error'")
    result: str | None = None


class ProposedAction(BaseModel):
    """A validated CREATE_CAMPAIGN proposal. This exact object is what a human approves, and what is created."""

    action_type: Literal["CREATE_CAMPAIGN"] = "CREATE_CAMPAIGN"
    name: str
    message: str
    category: str
    discount_percent: Decimal


class MemoryConflict(BaseModel):
    """A correction the manager made that contradicts an earlier note, waiting for review. Settle it with
    POST /ai/memories/{id}/accept (use the correction) or DELETE /ai/memories/{id} (keep the earlier note)."""

    id: uuid.UUID
    content: str
    evidence: str = Field(description="The manager's own words the correction came from")
    earlier_id: uuid.UUID | None = Field(description="The note it contradicts")
    earlier_content: str | None
    created_at: datetime


class ChatResponse(BaseModel):
    response: str
    tool_calls: list[ToolCallRecord]
    status: WorkflowStatus = Field(default="COMPLETED", description="COMPLETED for questions; see the workflow states")
    thread_id: str | None = Field(default=None, description="Workflow id for POST /ai/workflows/{thread_id}/resume")
    proposed_action: ProposedAction | None = None
    campaign: CampaignRead | None = Field(default=None, description="The campaign created when status is EXECUTED")
    conversation_id: uuid.UUID | None = Field(default=None, description="Send it back to continue the conversation")
    memory_conflicts: list[MemoryConflict] = Field(
        default_factory=list, description="Open memory conflicts from any conversation, newest first, at most 3"
    )


class ResumeRequest(BaseModel):
    """The only input a resume accepts. The proposal being decided on is the one stored with the workflow."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVED", "REJECTED"]
