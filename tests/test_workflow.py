from __future__ import annotations

import json
import unittest

from agentic_salmon.knowledge_port import KnowledgePort
from agentic_salmon.models import (
    AssertionSource,
    CandidateHypothesis,
    EntityKind,
    EntityMention,
    GateDecision,
    HumanEvidence,
    KnowledgeResult,
    McpCall,
    Observation,
    Perception,
    RelationKind,
    RelationshipMention,
    RetrievedChunk,
    Stage,
)
from agentic_salmon.perceive import PerceiveAgent
from agentic_salmon.reason import ReasonAgent
from agentic_salmon.workflow import AgenticSalmonWorkflow


class StaticProvider:
    def perceive(self) -> Perception:
        return Perception(
            observations=(
                Observation(
                    observation_id="visible_skin",
                    claim="Two food portions and a skin-like surface are shown.",
                    source="test fixture",
                ),
            ),
            unknowns=("verified food identity", "storage history", "allergens"),
            provenance="test fixture",
            candidate_hypotheses=(
                CandidateHypothesis(
                    "unresolved_food",
                    "unresolved food",
                    AssertionSource.HUMAN_HYPOTHESIS,
                    "An initial conjecture has no observable support.",
                ),
                CandidateHypothesis(
                    "fish",
                    "fish",
                    AssertionSource.HUMAN_HYPOTHESIS,
                    "Visible skin supports a fish hypothesis.",
                    ("visible_skin",),
                ),
                CandidateHypothesis(
                    "salmon",
                    "salmon",
                    AssertionSource.MODEL_ASSERTION,
                    "A prior model asserted salmon without label verification.",
                    ("visible_skin",),
                ),
            ),
            entity_mentions=(
                EntityMention(
                    mention_id="fish",
                    entity_kind=EntityKind.FOOD_PORTION,
                    label="visible food portions",
                    evidence_claim="Two food portions are visible.",
                ),
                EntityMention(
                    mention_id="basket",
                    entity_kind=EntityKind.COOKING_EQUIPMENT,
                    label="air-fryer basket",
                    evidence_claim="The portions are in an air-fryer basket.",
                ),
            ),
            relationship_mentions=(
                RelationshipMention(
                    subject_mention_id="fish",
                    relation_kind=RelationKind.LOCATED_IN,
                    object_mention_id="basket",
                    evidence_claim="The portions are located in the basket.",
                ),
            ),
        )


class StaticKnowledge(KnowledgePort):
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, top_k: int = 3) -> KnowledgeResult:
        self.queries.append(query)
        return KnowledgeResult(
            query=query,
            model="test-semantic-model",
            chunks=(
                RetrievedChunk(
                    chunk_id="temperature",
                    source_id="usda",
                    title="Air Fryers and Food Safety",
                    publisher="USDA FSIS",
                    url="https://example.test/usda",
                    text="Fish guidance uses a thermometer and 145 degrees Fahrenheit.",
                    score=0.94,
                    content_sha256="a" * 64,
                ),
                RetrievedChunk(
                    chunk_id="identity-limit",
                    source_id="policy",
                    title="Evidence Policy",
                    publisher="aixstudio",
                    url="docs/policy.md",
                    text="An image cannot verify species or food safety.",
                    score=0.88,
                    content_sha256="b" * 64,
                ),
            ),
            mcp_call=McpCall(
                server_name="test-mcp",
                server_version="1",
                protocol_version="test",
                tool_name="search_knowledge",
                duration_ms=1.2,
                success=True,
            ),
        )


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.knowledge = StaticKnowledge()
        self.workflow = AgenticSalmonWorkflow(
            PerceiveAgent(StaticProvider()),
            ReasonAgent(self.knowledge),
        )

    def test_initial_run_interrupts_at_review(self) -> None:
        result = self.workflow.run()

        self.assertEqual("interrupted", result.status)
        self.assertEqual(GateDecision.ASK, result.decision)
        self.assertIsNone(result.guidance)
        self.assertEqual(
            ["perceive", "connect", "reason"],
            [event.stage.value for event in result.events],
        )
        self.assertIn("retained_unknowns", result.interrupt)

    def test_approved_resume_runs_all_six_agents(self) -> None:
        result = self.workflow.run(
            HumanEvidence(
                selected_hypothesis="fish",
                accepts_retained_unknowns=True,
                authorizes_bounded_guidance=True,
                storage_recollection="retrieved frozen from home freezer",
                odor_observation="seasoning smelled reminiscent of lasagna",
            ),
            outcome="390°F for 14 minutes; wonderful and fresh-tasting",
            feedback="moderately spicy result preferred",
        )

        self.assertEqual("completed", result.status)
        self.assertEqual(GateDecision.ALLOW_BOUNDED, result.decision)
        self.assertIsNotNone(result.guidance)
        self.assertEqual(
            ["perceive", "connect", "reason", "review", "act", "learn"],
            [event.stage.value for event in result.events],
        )
        self.assertEqual(6, len(result.metrics))
        self.assertGreater(result.total_duration_ms, 0.0)
        self.assertIsNone(result.to_dict()["telemetry"]["token_usage"])
        mcp_call = result.events[2].evidence["knowledge"]["mcp_call"]
        self.assertEqual("test-mcp", mcp_call["server_name"])
        self.assertIn("air-fryer basket", self.knowledge.queries[0])
        review = result.events[3].evidence["review"]
        self.assertEqual(["verified food identity", "storage history", "allergens"], review["retained_unknowns"])

    def test_invalid_hypothesis_reinterrupts_and_resumes_same_run(self) -> None:
        invalid = self.workflow.run(
            HumanEvidence(
                selected_hypothesis="pizza",
                accepts_retained_unknowns=True,
                authorizes_bounded_guidance=True,
            )
        )

        self.assertEqual("interrupted", invalid.status)
        self.assertEqual(GateDecision.ASK, invalid.decision)
        self.assertEqual(
            ["perceive", "connect", "reason", "review"],
            [event.stage.value for event in invalid.events],
        )
        self.assertIn("hypothesis must be one of", invalid.interrupt["validation_messages"][0])

        resumed = self.workflow.resume(
            invalid.run_id,
            HumanEvidence(
                selected_hypothesis="fish",
                accepts_retained_unknowns=True,
                authorizes_bounded_guidance=True,
            ),
        )

        self.assertEqual(invalid.run_id, resumed.run_id)
        self.assertEqual(GateDecision.ALLOW_BOUNDED, resumed.decision)
        self.assertEqual("completed", resumed.status)
        self.assertEqual(Stage.LEARN, resumed.events[-1].stage)
        self.assertEqual(
            [1, 2, 3, 4, 5, 6, 7],
            [event.sequence for event in resumed.events],
        )
        self.assertEqual(
            [
                "perceive",
                "connect",
                "reason",
                "review",
                "review",
                "act",
                "learn",
            ],
            [event.stage.value for event in resumed.events],
        )

    def test_unknown_hypothesis_reinterrupts_before_act(self) -> None:
        result = self.workflow.run(
            HumanEvidence(
                selected_hypothesis="unresolved_food",
                accepts_retained_unknowns=True,
                authorizes_bounded_guidance=True,
            )
        )

        self.assertEqual("interrupted", result.status)
        self.assertEqual(GateDecision.ASK, result.decision)
        self.assertEqual(
            ["perceive", "connect", "reason", "review"],
            [event.stage.value for event in result.events],
        )
        self.assertIn("unknown hypothesis", result.interrupt["validation_messages"][0])

    def test_public_trace_keeps_assertion_sources_and_unresolved_mentions(self) -> None:
        result = self.workflow.run()
        perception = result.events[0].evidence

        self.assertNotIn("confidence", json.dumps(perception))
        self.assertEqual(
            ["human_hypothesis", "human_hypothesis", "model_assertion"],
            [item["source_kind"] for item in perception["candidate_hypotheses"]],
        )
        self.assertTrue(
            all("canonical_id" not in item for item in perception["entity_mentions"])
        )

        reason = result.events[2].evidence
        self.assertNotIn("portion shape", reason["query"].lower())
        self.assertEqual(
            ["unresolved_food", "fish", "salmon"],
            [item["hypothesis_id"] for item in reason["hypotheses"]],
        )

    def test_human_concern_stops_before_act(self) -> None:
        result = self.workflow.run(
            HumanEvidence(
                selected_hypothesis="fish",
                concern_reported=True,
                authorizes_bounded_guidance=False,
            )
        )

        self.assertEqual(GateDecision.STOP, result.decision)
        self.assertEqual("stopped", result.status)
        self.assertEqual(
            ["perceive", "connect", "reason", "review"],
            [event.stage.value for event in result.events],
        )

    def test_public_trace_excludes_private_reasoning_and_prompts(self) -> None:
        result = self.workflow.run()
        serialized = json.dumps(result.to_dict()).lower()

        self.assertNotIn("chain-of-thought", serialized)
        self.assertNotIn("hidden_reasoning", serialized)
        self.assertNotIn("system_prompt", serialized)


if __name__ == "__main__":
    unittest.main()
