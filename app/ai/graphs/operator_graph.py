# TODO: SECURITY — run llm-secure-patterns to apply LLM security patterns
"""Hybrid graph: the read-only agent investigates, a router sends the request to ANSWER or ACTION, and an ACTION is
planned, validated and then held for human approval. Only an approved proposal is executed (the campaign row is
created with status 'approved'); nothing is sent to customers or applied to prices.

    START -> agent_node -> decision_node --ANSWER--> END
                                         \\-ACTION--> planner_node -> validate_node --invalid--> END
                                                                                     \\-valid--> approval_node -> END

approval_node pauses with interrupt(); POST /ai/workflows/{id}/resume continues it with APPROVED or REJECTED.
If creating the campaign fails, the node fails before its checkpoint is written, so the workflow stays waiting and
the approval can be retried; the campaign id is the thread id, so a retry never creates a second campaign.
The proposal is produced once, by validate_node, and checkpointed. The resume carries only the decision, so what a
human approves is always the stored, validated proposal. State holds plain JSON values so it checkpoints cleanly.
"""

import json
import logging
import uuid
from typing import Literal, NotRequired, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from app.ai.actions import campaign as campaign_action
from app.ai.actions.campaign import ActionPlan, validate_plan
from app.ai.agents.business_agent import chat, utc_today
from app.ai.guardrails import BLOCKED_TEXT, action_claim, contact_details, trip, ungrounded_numbers
from app.ai.llm import structured_call
from app.ai.prompts import conversation, load_prompt, untrusted
from app.ai.routing import is_obviously_question
from app.ai.telemetry import llm_step
from app.core.errors import AIUnavailableError, AppError, ConflictError, NotFoundError
from app.schemas.ai import ChatResponse, ProposedAction

logger = logging.getLogger(__name__)

Route = Literal["ANSWER", "ACTION"]
ApprovalDecision = Literal["APPROVED", "REJECTED"]

WAITING_TEXT = "I prepared this action and need your approval before anything happens. Nothing has been changed yet."
EXECUTED_TEXT = (
    "Approved. The campaign was created with status 'approved'. Nothing was sent to customers and no prices changed."
)
REJECTED_TEXT = "Rejected. Nothing was changed."

DECISION_PROMPT = load_prompt("decision")
PLAN_PROMPT = load_prompt("plan")
APPROVAL_PROMPT = load_prompt("approval")

Verdict = Literal["APPROVE", "REJECT", "OTHER"]


class ApprovalVerdict(BaseModel):
    """Whether a chat message decides the pending proposal."""

    decision: Verdict = Field(
        description="APPROVE or REJECT only when the message clearly decides the pending proposal; otherwise OTHER"
    )


class Decision(BaseModel):
    """The router's verdict on what the user is asking for."""

    route: Route = Field(description="ANSWER for read-only questions, ACTION if any change is requested")


class OperatorState(TypedDict):
    message: str
    result: NotRequired[dict]  # ChatResponse payload so far; the last node to write it decides the final response
    decision: NotRequired[Route]
    plan: NotRequired[dict | None]
    proposal: NotRequired[dict]  # present only when the plan passed validation
    history: NotRequired[list[dict]]  # earlier conversation turns, oldest first: {"role", "content"}
    notes: NotRequired[str]  # the manager's saved notes for this turn; only the agent and the planner see them


def with_history(history: list[dict], text: str) -> str:
    """Prefix a structured-output call's input with the conversation so far (unchanged when there is none)."""
    if not history:
        return text
    return f"{conversation(history)}\n\n{text}"


def unsafe_reply(response: ChatResponse, state: OperatorState) -> str | None:
    """The first guardrail the agent's reply trips, if any. Allowed numbers come from this turn's tool results, the
    manager's own words (message, earlier turns, saved notes) and today's date: anything else the model made up."""
    if check := contact_details(response.response) or action_claim(response.response):
        return check
    sources = [
        *(c.result or "" for c in response.tool_calls), state["message"],
        *(turn["content"] for turn in state.get("history", [])), state.get("notes", ""), utc_today().isoformat(),
    ]
    return "ungrounded_number" if ungrounded_numbers(response.response, sources) else None


def route_after_agent(state: OperatorState) -> str:
    return END if state["result"].get("status") == "BLOCKED" else "decision_node"


def route_after_decision(state: OperatorState) -> Route:
    return "ANSWER" if state.get("decision") == "ANSWER" else "ACTION"  # unknown or missing -> the safe path


def route_after_validation(state: OperatorState) -> str:
    return "approval_node" if state.get("proposal") else END  # no validated proposal, nothing to approve


def build_graph(model: BaseChatModel, checkpointer: BaseCheckpointSaver, fast_model: BaseChatModel | None = None):
    """`fast_model` (default: `model`) serves the router; the agent and the planner always use `model`."""
    fast_model = fast_model or model

    async def agent_node(state: OperatorState) -> dict:
        with llm_step("agent"):
            response = await chat(  # read-only tools only
                model, state["message"], history=state.get("history", []), notes=state.get("notes", "")
            )
        result = response.model_dump(mode="json")
        if check := unsafe_reply(response, state):
            trip("reply", check, response.response)
            return {"result": result | {"status": "BLOCKED", "response": BLOCKED_TEXT}}
        return {"result": result}

    async def decision_node(state: OperatorState) -> dict:
        if is_obviously_question(state["message"], state.get("history", [])):  # no change word: skip the model call
            logger.info("router shortcut: ANSWER")
            return {"decision": "ANSWER"}
        human = with_history(state.get("history", []), untrusted("UNTRUSTED_USER_MESSAGE", state["message"]))
        verdict = await structured_call(fast_model, Decision, DECISION_PROMPT, human)
        # fail closed: an unusable verdict must never pass for a plain answer
        return {"decision": verdict.route if verdict else "ACTION"}

    async def planner_node(state: OperatorState) -> dict:
        # SECURITY: LLM01 (Prompt Injection) — Level B (Moderate). Pattern: system_prompt_template — the findings are
        # the agent's reply and can quote database text, so the request and findings sit inside UNTRUSTED tags and
        # PLAN_PROMPT treats them as data. validate_plan and human approval stay the real guard.
        # Applied by: llm-secure-patterns v1.0.0 / System Prompt Design, 2026-10-27.
        request = (f"{untrusted('UNTRUSTED_USER_REQUEST', state['message'])}\n\n"
                   f"{untrusted('UNTRUSTED_FINDINGS', state['result']['response'])}")
        if state.get("notes"):
            request += f"\n\n{state['notes']}"
        plan = await structured_call(model, ActionPlan, PLAN_PROMPT, with_history(state.get("history", []), request))
        return {"plan": plan.model_dump() if plan else None}

    async def validate_node(state: OperatorState) -> dict:
        plan = ActionPlan.model_validate(state["plan"]) if state.get("plan") else None
        proposal, errors = await validate_plan(plan)
        if proposal:
            return {"proposal": proposal.model_dump(mode="json")}
        reasons = "; ".join(errors)
        text = f"I couldn't prepare that action ({reasons}). Nothing was changed."
        return {"result": {**state["result"], "status": "INVALID_ACTION", "response": text}}

    async def approval_node(state: OperatorState, config: RunnableConfig) -> dict:
        decision = interrupt({"proposed_action": state["proposal"]})  # pauses here; resumed with APPROVED/REJECTED
        result = {**state["result"], "proposed_action": state["proposal"]}
        if decision == "APPROVED":
            campaign = await campaign_action.execute_proposal(  # module attribute, so tests can make it fail
                ProposedAction.model_validate(state["proposal"]), uuid.UUID(config["configurable"]["thread_id"])
            )
            return {"result": result | {"status": "EXECUTED", "response": EXECUTED_TEXT,
                                        "campaign": campaign.model_dump(mode="json")}}
        if decision == "REJECTED":
            return {"result": result | {"status": "REJECTED", "response": REJECTED_TEXT}}
        raise ValueError(f"Unexpected approval decision: {decision!r}")

    graph = StateGraph(OperatorState)
    graph.add_node("agent_node", agent_node)
    graph.add_node("decision_node", decision_node)
    graph.add_node("planner_node", planner_node)
    graph.add_node("validate_node", validate_node)
    graph.add_node("approval_node", approval_node)
    graph.add_edge(START, "agent_node")
    graph.add_conditional_edges("agent_node", route_after_agent, {"decision_node": "decision_node", END: END})
    graph.add_conditional_edges("decision_node", route_after_decision, {"ANSWER": END, "ACTION": "planner_node"})
    graph.add_edge("planner_node", "validate_node")
    graph.add_conditional_edges(
        "validate_node", route_after_validation, {"approval_node": "approval_node", END: END}
    )
    graph.add_edge("approval_node", END)
    return graph.compile(checkpointer=checkpointer)


def _is_waiting(snapshot) -> bool:
    """Paused at the approval step with its interrupt actually raised, i.e. a human has been shown the proposal."""
    return snapshot.next == ("approval_node",) and any(task.interrupts for task in snapshot.tasks)


def _response(snapshot, thread_id: str) -> ChatResponse:
    result = dict(snapshot.values["result"])
    if _is_waiting(snapshot):
        result |= {"status": "WAITING_APPROVAL", "response": WAITING_TEXT, "proposed_action": snapshot.values["proposal"]}
    if result.get("proposed_action"):
        result["thread_id"] = thread_id
    return ChatResponse.model_validate(result)


async def _run(graph, payload, thread_id: str) -> ChatResponse:
    config = {"configurable": {"thread_id": thread_id}}
    try:
        await graph.ainvoke(payload, config)
        return _response(await graph.aget_state(config), thread_id)
    except AppError:
        raise
    except Exception as exc:  # graph, routing or checkpoint-storage failure; the agent's own already arrive as AppError
        logger.exception("Operator graph failed")
        raise AIUnavailableError("The AI assistant could not complete the request. Try again shortly.") from exc


async def run_operator(
    model: BaseChatModel, checkpointer: BaseCheckpointSaver, message: str, history: list[dict] | None = None,
    notes: str = "", fast_model: BaseChatModel | None = None,
) -> ChatResponse:
    payload = {"message": message, "history": history or [], "notes": notes}
    return await _run(build_graph(model, checkpointer, fast_model), payload, str(uuid.uuid4()))


async def resume_operator(
    model: BaseChatModel, checkpointer: BaseCheckpointSaver, thread_id: str, decision: ApprovalDecision
) -> ChatResponse:
    graph = build_graph(model, checkpointer)
    try:
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    except Exception as exc:
        logger.exception("Could not read workflow %s", thread_id)
        raise AIUnavailableError("The workflow could not be loaded. Try again shortly.") from exc
    if not snapshot.values:
        raise NotFoundError(f"Workflow {thread_id} not found")
    if not _is_waiting(snapshot):
        status = snapshot.values.get("result", {}).get("status", "UNKNOWN")
        raise ConflictError(f"Workflow {thread_id} is not waiting for approval (status: {status})")
    return await _run(graph, Command(resume=decision), thread_id)


async def pending_proposal(checkpointer: BaseCheckpointSaver, thread_id: str) -> dict | None:
    """The workflow's stored proposal if it is waiting for approval, else None (decided, missing, or never paused)."""
    try:
        snapshot = await build_graph(None, checkpointer).aget_state({"configurable": {"thread_id": thread_id}})
    except Exception as exc:
        logger.exception("Could not read workflow %s", thread_id)
        raise AIUnavailableError("The workflow could not be loaded. Try again shortly.") from exc
    return snapshot.values["proposal"] if _is_waiting(snapshot) else None


async def classify_approval(model: BaseChatModel, proposal: dict, history: list[dict], message: str) -> Verdict:
    """Does `message` decide the pending proposal? Fails closed: an unusable answer is OTHER, never a decision."""
    pending = untrusted("UNTRUSTED_PENDING_PROPOSAL", json.dumps(proposal, ensure_ascii=False))
    human = with_history(history, f"{pending}\n\n{untrusted('UNTRUSTED_USER_MESSAGE', message)}")
    verdict = await structured_call(model, ApprovalVerdict, APPROVAL_PROMPT, human)
    return verdict.decision if verdict else "OTHER"
