import uuid
from datetime import UTC, datetime
from fastapi import APIRouter

from app.schemas.chat import ChatResponse, Memory, ResumeRequest

router = APIRouter(prefix="/ai", tags=["AI"])

# Mock data until the memory store and the approval workflow exist.
MOCK_MEMORIES = [
    Memory(id=uuid.UUID(int=1), kind="preference", content="Show the most recent errors first.",
           created_at=datetime(2026, 10, 29, tzinfo=UTC)),
    Memory(id=uuid.UUID(int=2), kind="observation", content="The payments service logs to /var/log/payments.log.",
           created_at=datetime(2026, 10, 29, tzinfo=UTC)),
]


@router.get("/memories", response_model=list[Memory], summary="List memories (mock data)")
async def list_memories() -> list[Memory]:
    return MOCK_MEMORIES


@router.post("/workflows/{thread_id}/resume", response_model=ChatResponse, summary="Approve or reject (mock reply)")
async def resume_workflow(thread_id: uuid.UUID, data: ResumeRequest) -> ChatResponse:
    approved = data.decision == "APPROVED"
    return ChatResponse(
        response=f"Mock reply: workflow {thread_id} {'approved' if approved else 'rejected'}.",
        status="EXECUTED" if approved else "REJECTED",
        conversation_id=uuid.uuid4(),
    )
