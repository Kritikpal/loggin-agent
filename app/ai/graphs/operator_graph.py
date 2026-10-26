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
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from app.ai.actions import campaign as campaign_action
from app.ai.actions.campaign import ActionPlan, validate_plan
from app.ai.agents.business_agent import chat
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

DECISION_PROMPT = """
Classify what the user is asking for.

ANSWER: they only want information or analysis (sales, inventory, trends, explanations, unrelated questions).
ACTION: they want anything changed or done (restock, reorder, create, update, delete, price changes, discounts,
campaigns, sending messages), even if they first ask for something to be checked or investigated.
Requests that mix a question with a change are ACTION.
"""

PLAN_PROMPT = """
Turn the user's request into one action.

CREATE_CAMPAIGN: a promotional campaign for one product category. Fill name, message, category and discount_percent
(a number such as 15 for 15%).
- Draft a short name and a customer-facing message if the user did not give them.
- Take category and discount_percent only from the user's words or the read-only findings. Never guess them:
  leave them null when they are not stated.
UNSUPPORTED: any other change (restocking, price changes, deleting, sending messages, ...). Say why in `reason`.
"""


APPROVAL_PROMPT = """
Decide whether the user's new message approves or rejects the pending proposal.

APPROVE: the message clearly says yes to this proposal (e.g. "yes", "go ahead", "approve it").
REJECT: the message clearly says no to this proposal (e.g. "no", "cancel it", "don't").
OTHER: anything else - questions, requests to change the proposal ("make it 10%"), new requests, or anything unclear.
"""

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


def with_history(history: list[dict], text: str) -> str:
    """Prefix a structured-output call's input with the conversation so far (unchanged when there is none)."""
    if not history:
        return text
    lines = "\n".join(f"{h['role']}: {h['content']}" for h in history)
    return f"Conversation so far:\n{lines}\n\n{text}"


def route_after_decision(state: OperatorState) -> Route:
    return "ANSWER" if state.get("decision") == "ANSWER" else "ACTION"  # unknown or missing -> the safe path


def route_after_validation(state: OperatorState) -> str:
    return "approval_node" if state.get("proposal") else END  # no validated proposal, nothing to approve


async def _structured[T: BaseModel](model: BaseChatModel, schema: type[T], system: str, human: str) -> T | None:
    """Structured-output call. None when the output is unusable: invalid, missing, or the call itself failed.
    json_schema, not the function-calling default: that forces a tool call, and Groq answers 400
    ("Tool choice is required") when the model just replies to something like "Hi"."""
    try:
        verdict = await model.with_structured_output(schema, method="json_schema").ainvoke(
            [SystemMessage(system), HumanMessage(human)]
        )
    except Exception:  # invalid structured output, provider error, ...
        logger.exception("%s call failed", schema.__name__)
        return None
    if not isinstance(verdict, schema):
        logger.warning("%s call returned no usable output: %r", schema.__name__, verdict)
        return None
    return verdict


def build_graph(model: BaseChatModel, checkpointer: BaseCheckpointSaver):
    async def agent_node(state: OperatorState) -> dict:
        response = await chat(model, state["message"], history=state.get("history", []))  # read-only tools only
        return {"result": response.model_dump(mode="json")}

    async def decision_node(state: OperatorState) -> dict:
        human = with_history(state.get("history", []), state["message"])
        verdict = await _structured(model, Decision, DECISION_PROMPT, human)
        # fail closed: an unusable verdict must never pass for a plain answer
        return {"decision": verdict.route if verdict else "ACTION"}

    async def planner_node(state: OperatorState) -> dict:
        request = f"Request:\n{state['message']}\n\nRead-only findings:\n{state['result']['response']}"
        plan = await _structured(model, ActionPlan, PLAN_PROMPT, with_history(state.get("history", []), request))
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
    graph.add_edge("agent_node", "decision_node")
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
    model: BaseChatModel, checkpointer: BaseCheckpointSaver, message: str, history: list[dict] | None = None
) -> ChatResponse:
    payload = {"message": message, "history": history or []}
    return await _run(build_graph(model, checkpointer), payload, str(uuid.uuid4()))


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
    human = with_history(history, f"Pending proposal:\n{json.dumps(proposal)}\n\nNew message:\n{message}")
    verdict = await _structured(model, ApprovalVerdict, APPROVAL_PROMPT, human)
    return verdict.decision if verdict else "OTHER"
