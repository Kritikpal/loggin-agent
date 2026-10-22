import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from langchain_core.language_models import BaseChatModel
from langchain_groq import ChatGroq
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.ai.graphs.operator_graph import resume_operator, run_operator
from app.core.config import get_settings
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


ChatModelDep = Annotated[BaseChatModel, Depends(chat_model)]
CheckpointerDep = Annotated[BaseCheckpointSaver, Depends(get_checkpointer)]


@router.post("/chat", response_model=ChatResponse, summary="Ask the sales assistant a question")
async def ai_chat(data: ChatRequest, model: ChatModelDep, checkpointer: CheckpointerDep) -> ChatResponse:
    """Questions are answered directly. A request to change something is planned and validated, then returned as
    `WAITING_APPROVAL` with a `thread_id`; nothing happens until it is approved via the resume endpoint."""
    return await run_operator(model, checkpointer, data.message)


@router.post(
    "/workflows/{thread_id}/resume",
    response_model=ChatResponse,
    summary="Approve or reject a proposed action",
    responses={404: {"description": "Unknown workflow"}, 409: {"description": "Workflow is not waiting for approval"}},
)
async def resume_workflow(
    thread_id: uuid.UUID, data: ResumeRequest, model: ChatModelDep, checkpointer: CheckpointerDep
) -> ChatResponse:
    """APPROVED -> `APPROVED_PENDING_EXECUTION` (execution is not implemented); REJECTED -> `REJECTED`. The decision
    applies to the exact proposal that was validated and returned by the chat call."""
    return await resume_operator(model, checkpointer, str(thread_id), data.decision)
