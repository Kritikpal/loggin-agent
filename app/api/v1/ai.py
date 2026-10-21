from typing import Annotated

from fastapi import APIRouter, Depends
from langchain_core.language_models import BaseChatModel
from langchain_groq import ChatGroq

from app.ai.agents.business_agent import chat
from app.core.config import get_settings
from app.core.errors import AIUnavailableError
from app.schemas.ai import ChatRequest, ChatResponse

router = APIRouter(prefix="/ai", tags=["AI"])


def chat_model() -> BaseChatModel:
    settings = get_settings()
    if not settings.groq_api_key:
        raise AIUnavailableError("The AI assistant is not configured (GROQ_API_KEY is missing).")
    return ChatGroq(model=settings.groq_model, api_key=settings.groq_api_key, temperature=0, timeout=30, max_retries=1)


ChatModelDep = Annotated[BaseChatModel, Depends(chat_model)]


@router.post("/chat", response_model=ChatResponse, summary="Ask the sales assistant a question")
async def ai_chat(data: ChatRequest, model: ChatModelDep) -> ChatResponse:
    return await chat(model, data.message)
