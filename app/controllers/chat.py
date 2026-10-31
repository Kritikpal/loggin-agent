import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from app.agent.graph import Agent, ask
from app.controllers.dependencies import get_agent
from app.schemas.chat import ChatRequest, ChatResponse, Memory, ResumeRequest

router = APIRouter(prefix="/ai", tags=["AI"])

# Mock data until the memory store and the approval workflow exist.
MOCK_MEMORIES = [
    Memory(id=uuid.UUID(int=1), kind="preference", content="Show the most recent errors first.",
           created_at=datetime(2026, 10, 29, tzinfo=UTC)),
    Memory(id=uuid.UUID(int=2), kind="observation", content="The payments service logs to /var/log/payments.log.",
           created_at=datetime(2026, 10, 29, tzinfo=UTC)),
]


@router.post("/chat", response_model=ChatResponse, summary="Ask the log investigation agent")
async def ai_chat(data: ChatRequest, agent: Annotated[Agent, Depends(get_agent)]) -> ChatResponse:
    # ponytail: every question starts fresh; conversation_id is echoed, not remembered. Add a checkpointer when
    # follow-up questions are needed.
    reply = await ask(agent, data.message)
    return ChatResponse(
        response=reply.answer, tool_calls=reply.tool_calls, conversation_id=data.conversation_id or uuid.uuid4())


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
