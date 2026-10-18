"""LangGraph harness for the guarded six-agent Agentic Salmon workflow."""

from __future__ import annotations

import asyncio
import operator
import uuid
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .act import ActAgent
from .connect import ConnectAgent
from .guard import GuardPolicy
from .learn import LearnAgent
from .models import (
    ActionResult,
    ConnectedEvent,
    GateDecision,
    HumanEvidence,
    LearnResult,
    NodeMetric,
    Perception,
    PublicEvent,
    ReasonResult,
    ReviewResult,
    RunResult,
    Stage,
)
from .ontology import SALMON_ONTOLOGY
from .perceive import PerceiveAgent
from .reason import ReasonAgent
from .review import ReviewAgent


class WorkflowState(TypedDict, total=False):
    run_id: str
    perception: Perception
    connected_event: ConnectedEvent
    reason_result: ReasonResult
    review_result: ReviewResult
    action_result: ActionResult
    learn_result: LearnResult
    outcome: str | None
    feedback: str | None
    events: Annotated[list[PublicEvent], operator.add]
    metrics: Annotated[list[NodeMetric], operator.add]


class AgenticSalmonWorkflow:
    """Bind independent agent contracts to a resumable LangGraph execution."""

    def __init__(
        self,
        perceive: PerceiveAgent,
        reason: ReasonAgent,
        *,
        connect: ConnectAgent | None = None,
        review: ReviewAgent | None = None,
        act: ActAgent | None = None,
        learn: LearnAgent | None = None,
        guard: GuardPolicy | None = None,
        checkpointer: Any = None,
        defer_action: bool = False,
    ) -> None:
        self.perceive_agent = perceive
        self.connect_agent = connect or ConnectAgent(SALMON_ONTOLOGY)
        self.reason_agent = reason
        self.review_agent = review or ReviewAgent()
        self.act_agent = act or ActAgent()
        self.learn_agent = learn or LearnAgent()
        self.guard = guard or GuardPolicy()
        self.checkpointer = checkpointer
        self.defer_action = defer_action
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(WorkflowState)
        builder.add_node(Stage.PERCEIVE.value, self._perceive_node)
        builder.add_node(Stage.CONNECT.value, self._connect_node)
        builder.add_node(Stage.REASON.value, self._reason_node)
        builder.add_node(Stage.REVIEW.value, self._review_node)
        builder.add_node(Stage.ACT.value, self._act_node)
        builder.add_node(Stage.LEARN.value, self._learn_node)
        if self.defer_action:
            builder.add_node("release", self._release_node)
            builder.add_edge("release", Stage.ACT.value)
        builder.add_edge(START, Stage.PERCEIVE.value)
        builder.add_edge(Stage.PERCEIVE.value, Stage.CONNECT.value)
        builder.add_edge(Stage.CONNECT.value, Stage.REASON.value)
        builder.add_edge(Stage.REASON.value, Stage.REVIEW.value)
        builder.add_conditional_edges(
            Stage.REVIEW.value,
            self._route_review,
            {
                Stage.ACT.value: "release" if self.defer_action else Stage.ACT.value,
                Stage.REVIEW.value: Stage.REVIEW.value,
                "end": END,
            },
        )
        builder.add_edge(Stage.ACT.value, Stage.LEARN.value)
        builder.add_edge(Stage.LEARN.value, END)
        trusted_models = (
            "ActionResult",
            "AssertionSource",
            "CandidateHypothesis",
            "ConnectedEvent",
            "EntityKind",
            "EntityMention",
            "GateDecision",
            "Hypothesis",
            "HypothesisStatus",
            "KnowledgeResult",
            "LearnResult",
            "McpCall",
            "NodeMetric",
            "Observation",
            "Perception",
            "PublicEvent",
            "ReasonResult",
            "RelationKind",
            "RelationshipMention",
            "ResolvedEntity",
            "ResolvedRelationship",
            "RetrievedChunk",
            "ReviewResult",
            "Stage",
        )
        serde = JsonPlusSerializer(
            allowed_msgpack_modules=[
                ("agentic_salmon.models", name) for name in trusted_models
            ]
        )
        saver = self.checkpointer or InMemorySaver(serde=serde)
        saver.serde = serde
        return builder.compile(checkpointer=saver)

    def _release_node(self, state: WorkflowState) -> dict:
        self.guard.require_transition(Stage.ACT, review=state["review_result"])
        response = interrupt({"phase": "action_ready", "question": "Adjudication saved. Run Proof-3 to release the bounded action."})
        if response != {"execute": True}:
            raise PermissionError("Action release requires Proof-3 execution")
        return {}

    async def aexecute(self, run_id: str) -> RunResult:
        config = {"configurable": {"thread_id": run_id}}
        result = await self.graph.ainvoke(Command(resume={"execute": True}), config=config)
        return _run_result(run_id, result, _interrupt_payload(result))

    async def arun(
        self,
        review_response: HumanEvidence | None = None,
        *,
        run_id: str | None = None,
        outcome: str | None = None,
        feedback: str | None = None,
    ) -> RunResult:
        active_run_id = run_id or str(uuid.uuid4())
        result = await self.astart(
            run_id=active_run_id,
            outcome=outcome,
            feedback=feedback,
        )
        if result.interrupt is not None and review_response is not None:
            result = await self.aresume(active_run_id, review_response)
        return result

    async def astart(
        self,
        *,
        run_id: str | None = None,
        outcome: str | None = None,
        feedback: str | None = None,
    ) -> RunResult:
        active_run_id = run_id or str(uuid.uuid4())
        config = {"configurable": {"thread_id": active_run_id}}
        result = await self.graph.ainvoke(
            {
                "run_id": active_run_id,
                "outcome": outcome,
                "feedback": feedback,
                "events": [],
                "metrics": [],
            },
            config=config,
        )
        interrupt_payload = _interrupt_payload(result)
        return _run_result(active_run_id, result, interrupt_payload)

    async def aresume(
        self,
        run_id: str,
        review_response: HumanEvidence,
    ) -> RunResult:
        config = {"configurable": {"thread_id": run_id}}
        result = await self.graph.ainvoke(
            Command(resume=review_response.to_dict()),
            config=config,
        )
        return _run_result(run_id, result, _interrupt_payload(result))

    def run(
        self,
        review_response: HumanEvidence | None = None,
        *,
        run_id: str | None = None,
        outcome: str | None = None,
        feedback: str | None = None,
    ) -> RunResult:
        return asyncio.run(
            self.arun(
                review_response,
                run_id=run_id,
                outcome=outcome,
                feedback=feedback,
            )
        )

    def resume(self, run_id: str, review_response: HumanEvidence) -> RunResult:
        return asyncio.run(self.aresume(run_id, review_response))

    def _perceive_node(self, state: WorkflowState) -> WorkflowState:
        started_at, started = _start_metric()
        perception = self.perceive_agent.perceive()
        return {
            "perception": perception,
            "events": [
                PublicEvent(
                    sequence=_next_sequence(state),
                    stage=Stage.PERCEIVE,
                    summary="Separated sourced observations from hypotheses and unknowns.",
                    evidence=perception.to_dict(),
                )
            ],
            "metrics": [_finish_metric(Stage.PERCEIVE, started_at, started)],
        }

    def _connect_node(self, state: WorkflowState) -> WorkflowState:
        self.guard.require_transition(Stage.CONNECT, has_perception="perception" in state)
        started_at, started = _start_metric()
        connected = self.connect_agent.connect(state["perception"])
        return {
            "connected_event": connected,
            "events": [
                PublicEvent(
                    sequence=_next_sequence(state),
                    stage=Stage.CONNECT,
                    summary="Resolved mentions and assembled an ontology-valid event.",
                    evidence=connected.to_dict(),
                )
            ],
            "metrics": [_finish_metric(Stage.CONNECT, started_at, started)],
        }

    async def _reason_node(self, state: WorkflowState) -> WorkflowState:
        self.guard.require_transition(
            Stage.REASON,
            has_connected_event="connected_event" in state,
        )
        started_at, started = _start_metric()
        reason = await self.reason_agent.reason(
            state["perception"],
            state["connected_event"],
        )
        return {
            "reason_result": reason,
            "events": [
                PublicEvent(
                    sequence=_next_sequence(state),
                    stage=Stage.REASON,
                    summary="Called semantic RAG through MCP and produced a cited proposal.",
                    evidence=reason.to_dict(),
                )
            ],
            "metrics": [_finish_metric(Stage.REASON, started_at, started)],
        }

    def _review_node(self, state: WorkflowState) -> WorkflowState:
        self.guard.require_transition(Stage.REVIEW, has_reason_result="reason_result" in state)
        prior_review = state.get("review_result")
        response = interrupt(
            {
                "phase": "human_review",
                "question": "Select a working hypothesis and authorize only bounded guidance.",
                "validation_messages": (
                    list(prior_review.reasons)
                    if prior_review is not None
                    and prior_review.decision is GateDecision.ASK
                    else []
                ),
                "required_fields": [
                    "selected_hypothesis",
                    "accepts_retained_unknowns",
                    "authorizes_bounded_guidance",
                ],
                "retained_unknowns": list(state["reason_result"].retained_unknowns),
                "offered_hypotheses": [
                    {
                        "hypothesis_id": item.hypothesis_id,
                        "label": item.name,
                        "status": item.status.value,
                        "action_eligible": item.status.value != "unknown",
                    }
                    for item in state["reason_result"].hypotheses
                ],
                "allowed_decisions": ["ASK", "STOP", "ALLOW_BOUNDED"],
            }
        )
        started_at, started = _start_metric()
        evidence = HumanEvidence.from_dict(response)
        review = self.review_agent.review(state["reason_result"], evidence)
        return {
            "review_result": review,
            "events": [
                PublicEvent(
                    sequence=_next_sequence(state),
                    stage=Stage.REVIEW,
                    summary=f"Human review returned {review.decision.value.upper()}.",
                    evidence={
                        "human_evidence": evidence.to_dict(),
                        "review": review.to_dict(),
                    },
                )
            ],
            "metrics": [_finish_metric(Stage.REVIEW, started_at, started)],
        }

    def _route_review(self, state: WorkflowState) -> str:
        review = state["review_result"]
        if review.decision is GateDecision.ALLOW_BOUNDED:
            return Stage.ACT.value
        if review.decision is GateDecision.ASK:
            return Stage.REVIEW.value
        return "end"

    def _act_node(self, state: WorkflowState) -> WorkflowState:
        review = state["review_result"]
        self.guard.require_transition(Stage.ACT, review=review)
        started_at, started = _start_metric()
        action = self.act_agent.act(state["reason_result"], review)
        return {
            "action_result": action,
            "events": [
                PublicEvent(
                    sequence=_next_sequence(state),
                    stage=Stage.ACT,
                    summary="Released only the human-authorized bounded intervention.",
                    evidence=action.to_dict(),
                )
            ],
            "metrics": [_finish_metric(Stage.ACT, started_at, started)],
        }

    def _learn_node(self, state: WorkflowState) -> WorkflowState:
        self.guard.require_transition(Stage.LEARN, has_action="action_result" in state)
        started_at, started = _start_metric()
        learning = self.learn_agent.learn(
            state["action_result"],
            outcome=state.get("outcome"),
            feedback=state.get("feedback"),
        )
        return {
            "learn_result": learning,
            "events": [
                PublicEvent(
                    sequence=_next_sequence(state),
                    stage=Stage.LEARN,
                    summary="Recorded outcome evidence without changing policy.",
                    evidence=learning.to_dict(),
                )
            ],
            "metrics": [_finish_metric(Stage.LEARN, started_at, started)],
        }


def _start_metric() -> tuple[str, float]:
    return datetime.now(UTC).isoformat(), perf_counter()


def _next_sequence(state: WorkflowState) -> int:
    return len(state.get("events", [])) + 1


def _finish_metric(stage: Stage, started_at: str, started: float) -> NodeMetric:
    return NodeMetric(
        stage=stage,
        started_at=started_at,
        ended_at=datetime.now(UTC).isoformat(),
        duration_ms=round((perf_counter() - started) * 1000, 3),
    )


def _interrupt_payload(state: dict[str, Any]) -> dict[str, Any] | None:
    values = state.get("__interrupt__")
    if not values:
        return None
    first = values[0]
    value = getattr(first, "value", first)
    return value if isinstance(value, dict) else {"value": value}


def _run_result(
    run_id: str,
    state: WorkflowState | dict[str, Any],
    interrupt_payload: dict[str, Any] | None,
) -> RunResult:
    review = state.get("review_result")
    action = state.get("action_result")
    decision = review.decision if review is not None else GateDecision.ASK
    if interrupt_payload is not None and interrupt_payload.get("phase") == "action_ready":
        status = "adjudicated"
    elif interrupt_payload is not None:
        status = "interrupted"
    elif decision is GateDecision.STOP:
        status = "stopped"
    else:
        status = "completed"
    return RunResult(
        run_id=run_id,
        status=status,
        decision=decision,
        guidance=action.guidance if action is not None else None,
        events=tuple(state.get("events", [])),
        metrics=tuple(state.get("metrics", [])),
        interrupt=interrupt_payload,
    )
