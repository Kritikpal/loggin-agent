import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.conversation import chat_turn, record_button_decision
from app.ai.graphs.operator_graph import resume_operator
from app.ai.llm import chat_model, fast_chat_model
from app.api.deps import CONFLICT, NOT_FOUND, MemoryServiceDep, SessionDep
from app.core.database import SessionLocal
from app.core.errors import AIUnavailableError
from app.schemas.ai import ChatRequest, ChatResponse, ResumeRequest
from app.schemas.memory import MemoryKind, MemoryRead, MemoryStatus

router = APIRouter(prefix="/ai", tags=["AI"])


def get_checkpointer(request: Request) -> BaseCheckpointSaver:
    saver = getattr(request.app.state, "checkpointer", None)
    if saver is None:
        raise AIUnavailableError("The AI workflow storage is unavailable (see the server log); restart after fixing it.")
    return saver


def memory_sessions() -> async_sessionmaker[AsyncSession] | None:
    """Where long-term memory is saved from the background; None turns saving off (tests override it)."""
    return SessionLocal


ChatModelDep = Annotated[BaseChatModel, Depends(chat_model)]
FastModelDep = Annotated[BaseChatModel, Depends(fast_chat_model)]
# Built separately (use_cache=False), so the background memory extraction takes the next API key, not the agent's.
MemoryModelDep = Annotated[BaseChatModel, Depends(chat_model, use_cache=False)]
CheckpointerDep = Annotated[BaseCheckpointSaver, Depends(get_checkpointer)]
MemorySessionsDep = Annotated[async_sessionmaker[AsyncSession] | None, Depends(memory_sessions)]


@router.post("/chat", response_model=ChatResponse, summary="Ask the sales assistant a question")
async def ai_chat(
    data: ChatRequest,
    session: SessionDep,
    model: ChatModelDep,
    fast_model: FastModelDep,
    memory_model: MemoryModelDep,
    checkpointer: CheckpointerDep,
    background: BackgroundTasks,
    sessions: MemorySessionsDep,
) -> ChatResponse:
    """Questions are answered directly. A request to change something is planned and validated, then returned as
    `WAITING_APPROVAL` with a `thread_id`; nothing happens until it is approved, either with a clear yes/no in the
    same conversation or via the resume endpoint. Send `conversation_id` back to continue a conversation.
    Lasting preferences and observations the manager states are saved to memory after the reply is sent."""
    return await chat_turn(
        session, model, checkpointer, data.message, data.conversation_id, background, sessions,
        fast_model=fast_model, memory_model=memory_model,
    )


@router.get("/memories", response_model=list[MemoryRead], summary="List what the assistant remembers (newest first)")
async def list_memories(
    service: MemoryServiceDep, kind: MemoryKind | None = None, status: MemoryStatus | None = None
) -> list[MemoryRead]:
    """Up to 100 memories. Without `status`, the active and conflicted ones (those that can still affect a chat)."""
    return await service.list(kind, status)


@router.delete(
    "/memories/{memory_id}", status_code=204, responses=NOT_FOUND,
    summary="Forget a memory (rejects it if it is a flagged conflict)",
)
async def forget_memory(memory_id: uuid.UUID, service: MemoryServiceDep) -> None:
    await service.forget(memory_id)


@router.post(
    "/memories/{memory_id}/accept", response_model=MemoryRead, responses=NOT_FOUND | CONFLICT,
    summary="Accept a flagged conflict: the new memory replaces the one it contradicted",
)
async def accept_memory(memory_id: uuid.UUID, service: MemoryServiceDep) -> MemoryRead:
    return await service.accept(memory_id)


@router.post(
    "/workflows/{thread_id}/resume",
    response_model=ChatResponse,
    summary="Approve or reject a proposed action",
    responses={404: {"description": "Unknown workflow"}, 409: {"description": "Workflow is not waiting for approval"}},
)
async def resume_workflow(
    thread_id: uuid.UUID,
    data: ResumeRequest,
    session: SessionDep,
    model: ChatModelDep,
    checkpointer: CheckpointerDep,
) -> ChatResponse:
    """APPROVED -> `EXECUTED`: the campaign is created with status 'approved' and returned in `campaign` (nothing is
    sent to customers). REJECTED -> `REJECTED`. The decision applies to the exact proposal that was validated and
    returned by the chat call. The outcome is also added to the conversation the proposal came from."""
    response = await resume_operator(model, checkpointer, str(thread_id), data.decision)
    conversation_id = await record_button_decision(session, thread_id, response)
    return response.model_copy(update={"conversation_id": conversation_id})
