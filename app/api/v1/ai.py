import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from langchain_core.language_models import BaseChatModel
from langchain_groq import ChatGroq
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.conversation import chat_turn, record_button_decision
from app.ai.graphs.operator_graph import resume_operator
from app.api.deps import SessionDep
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.errors import AIUnavailableError
from app.schemas.ai import ChatRequest, ChatResponse, ResumeRequest

router = APIRouter(prefix="/ai", tags=["AI"])


def chat_model() -> BaseChatModel:
    settings = get_settings()
    if not settings.groq_api_key:
        raise AIUnavailableError("The AI assistant is not configured (GROQ_API_KEY is missing).")
    return ChatGroq(model=settings.groq_model, api_key=settings.groq_api_key, temperature=0, timeout=30, max_retries=1)


def get_checkpointer(request: Request) -> BaseCheckpointSaver:
    saver = getattr(request.app.state, "checkpointer", None)
    if saver is None:
        raise AIUnavailableError("The AI workflow storage is unavailable (see the server log); restart after fixing it.")
    return saver


def memory_sessions() -> async_sessionmaker[AsyncSession] | None:
    """Where long-term memory is saved from the background; None turns saving off (tests override it)."""
    return SessionLocal


ChatModelDep = Annotated[BaseChatModel, Depends(chat_model)]
CheckpointerDep = Annotated[BaseCheckpointSaver, Depends(get_checkpointer)]
MemorySessionsDep = Annotated[async_sessionmaker[AsyncSession] | None, Depends(memory_sessions)]


@router.post("/chat", response_model=ChatResponse, summary="Ask the sales assistant a question")
async def ai_chat(
    data: ChatRequest,
    session: SessionDep,
    model: ChatModelDep,
    checkpointer: CheckpointerDep,
    background: BackgroundTasks,
    sessions: MemorySessionsDep,
) -> ChatResponse:
    """Questions are answered directly. A request to change something is planned and validated, then returned as
    `WAITING_APPROVAL` with a `thread_id`; nothing happens until it is approved, either with a clear yes/no in the
    same conversation or via the resume endpoint. Send `conversation_id` back to continue a conversation.
    Lasting preferences and observations the manager states are saved to memory after the reply is sent."""
    return await chat_turn(session, model, checkpointer, data.message, data.conversation_id, background, sessions)


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
