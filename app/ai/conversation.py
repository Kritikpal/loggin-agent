"""One /ai/chat turn inside a conversation: load recent history, run the workflow, store the turn only if it
succeeded. Lives in app/ai (not app/services) because it needs the model and the checkpointer."""

import logging
import uuid

from fastapi import BackgroundTasks
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.guardrails import BLOCKED_TEXT, injection, trip
from app.ai.graphs.operator_graph import classify_approval, pending_proposal, resume_operator, run_operator
from app.ai.memory import open_conflicts, recall, remember
from app.core.errors import NotFoundError
from app.models import ChatMessage
from app.repositories.chat_message import ChatMessageRepository
from app.schemas.ai import ChatResponse

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 10  # 5 exchanges; summarise older turns if context ever grows too large


def stored_reply(response: ChatResponse) -> str:
    """What the conversation remembers of a reply: the text, plus the proposal or campaign it refers to, so later
    messages like "that campaign" have something to resolve against."""
    p, c = response.proposed_action, response.campaign
    if response.status == "WAITING_APPROVAL" and p:
        return f"{response.response}\nProposed campaign: {p.name} | {p.category} | {p.discount_percent}% | message: {p.message}"
    if response.status == "EXECUTED" and c:
        return f"{response.response}\nCreated campaign: {c.name} (id {c.id})"
    return response.response


async def chat_turn(
    session: AsyncSession,
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver,
    message: str,
    conversation_id: uuid.UUID | None,
    background: BackgroundTasks | None = None,
    memory_sessions: async_sessionmaker[AsyncSession] | None = None,
    fast_model: BaseChatModel | None = None,
    memory_model: BaseChatModel | None = None,
) -> ChatResponse:
    """With `background` and `memory_sessions`, what the manager said is also offered to `remember` once the reply
    has been sent. `fast_model` (default: `model`) serves the router and the approval check; `memory_model`
    (default: `model`) the background extraction."""
    if check := injection(message):  # before any model call; the turn is not stored or offered to memory
        trip("input", check, message, conversation_id)
        return ChatResponse(response=BLOCKED_TEXT, tool_calls=[], status="BLOCKED", conversation_id=conversation_id)
    fast_model = fast_model or model
    repo = ChatMessageRepository(session)
    history: list[dict] = []
    thread = None
    if conversation_id is None:
        conversation_id = uuid.uuid4()
    else:
        earlier = await repo.recent(conversation_id, MAX_HISTORY_MESSAGES)
        if not earlier:
            raise NotFoundError(f"Conversation {conversation_id} not found")
        history = [{"role": m.role, "content": m.content} for m in earlier]
        thread = await repo.latest_thread_id(conversation_id)
    conflicts = await open_conflicts(session, conversation_id)  # flagged on earlier turns, in any conversation
    await session.commit()  # end the read transaction: no connection is held during model calls

    response = None
    proposal = await pending_proposal(checkpointer, str(thread)) if thread else None
    if proposal:  # the newest proposal is still waiting: does this message decide it?
        verdict = await classify_approval(fast_model, proposal, history, message)
        if verdict != "OTHER":
            decision = "APPROVED" if verdict == "APPROVE" else "REJECTED"
            response = await resume_operator(model, checkpointer, str(thread), decision)
    if response is None:
        notes = await recall(session, message, history, conversation_id)
        await session.commit()  # end the recall read transaction: no connection is held during model calls
        response = await run_operator(model, checkpointer, message, history, notes, fast_model=fast_model)

    thread_id = uuid.UUID(response.thread_id) if response.thread_id else None
    reply = stored_reply(response)
    await _save(
        session,
        ChatMessage(conversation_id=conversation_id, role="user", content=message),
        ChatMessage(conversation_id=conversation_id, role="assistant", content=reply, thread_id=thread_id),
    )
    if background is not None and memory_sessions is not None:  # runs after the response is sent
        background.add_task(
            remember, memory_model or model, memory_sessions, message=message, reply=reply, status=response.status,
            tool_results="\n".join(c.result or "" for c in response.tool_calls), history=history,
            conversation_id=conversation_id,
        )
    return response.model_copy(update={"conversation_id": conversation_id, "memory_conflicts": conflicts})


async def _save(session: AsyncSession, *messages: ChatMessage) -> None:
    """Memory is best-effort: by now the turn's real effect (an answer, a proposal, a created campaign) has happened,
    so a failed save is logged instead of turning that result into an error. The next turn just remembers less."""
    try:
        ChatMessageRepository(session).add(*messages)
        await session.commit()
    except Exception:
        logger.exception("Could not save chat messages for conversation %s", messages[0].conversation_id)
        await session.rollback()


async def record_button_decision(session: AsyncSession, thread_id: uuid.UUID, response: ChatResponse) -> uuid.UUID | None:
    """After an approval-button decision, tell the conversation the proposal came from (if any) what happened, so
    later turns don't think it is still waiting. Returns that conversation's id."""
    repo = ChatMessageRepository(session)
    conversation_id = await repo.conversation_for_thread(thread_id)
    if conversation_id is None:  # started outside /ai/chat (e.g. before conversations existed)
        return None
    c = response.campaign
    if response.status == "EXECUTED" and c:
        content = f"Approved with the approval button. Campaign '{c.name}' was created (id {c.id})."
    else:
        content = "Rejected with the approval button. Nothing was changed."
    await _save(session, ChatMessage(conversation_id=conversation_id, role="assistant", content=content, thread_id=thread_id))
    return conversation_id
