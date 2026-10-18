from __future__ import annotations
import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agentic_salmon.cli import main
from agentic_salmon.proofs import ProofStore, perform, review_packet
from test_workflow import StaticKnowledge, StaticProvider


class NeverProvider:
    def perceive(self):
        raise AssertionError("Perceive reran during durable pickup")


class NeverKnowledge:
    async def search(self, *args, **kwargs):
        raise AssertionError("RAG reran during durable pickup")


class DurableProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProofStore(Path(self.temp.name), "test-001")

    def start(self):
        return asyncio.run(perform("proof-1", self.store, provider=StaticProvider(), knowledge=StaticKnowledge()))

    def resume(self, command, decision=None):
        # A new graph, checkpointer connection and event loop for every command.
        store = ProofStore(Path(self.temp.name), "test-001")
        return asyncio.run(perform(command, store, decision, provider=NeverProvider(), knowledge=NeverKnowledge()))

    def test_three_proofs_pick_up_durably_without_rerunning_evidence(self):
        first = self.start()
        self.assertEqual(3, len(first.events))
        self.assertIsNone(first.guidance)
        packet = review_packet(self.store.read("paused"))
        self.assertIn("APPROVE selects fish", packet)
        second = self.resume("proof-2", "APPROVE")
        self.assertEqual("adjudicated", second.status)
        self.assertEqual(4, len(second.events))
        self.assertIsNone(second.guidance)
        self.assertEqual(first.events, second.events[:3])
        self.assertEqual(first.events[2].evidence["retained_unknowns"], second.events[3].evidence["review"]["retained_unknowns"])
        third = self.resume("proof-3")
        self.assertEqual("completed", third.status)
        self.assertEqual(["perceive", "connect", "reason", "review", "act", "learn"], [e.stage.value for e in third.events])
        self.assertIn("Historical case record", third.events[-1].evidence["outcome"])
        with self.assertRaisesRegex(ValueError, "completed"):
            self.resume("proof-3")
        records = [json.loads(line) for line in self.store.log.read_text().splitlines() if line.startswith("{")]
        self.assertEqual(["paused", "adjudicated", "completed"], [r["phase"] for r in records])

    def test_wrong_order_duplicate_start_and_duplicate_review_are_blocked(self):
        with self.assertRaisesRegex(ValueError, "unknown run"):
            self.resume("proof-3")
        self.start()
        with self.assertRaisesRegex(ValueError, "paused"):
            self.resume("proof-3")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.start()
        self.resume("proof-2", "APPROVE")
        with self.assertRaisesRegex(ValueError, "adjudicated"):
            self.resume("proof-2", "APPROVE")

    def test_stop_is_durable_and_prevents_action(self):
        self.start()
        result = self.resume("proof-2", "STOP")
        self.assertEqual("stopped", result.status)
        self.assertIsNone(result.guidance)
        with self.assertRaisesRegex(ValueError, "stopped"):
            self.resume("proof-3")

    def test_invalid_input_preserves_paused_checkpoint(self):
        self.start()
        out = io.StringIO()
        with patch("builtins.input", return_value="guess"), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = main(["proof-2", "--run-id", "test-001", "--log-directory", self.temp.name])
        self.assertEqual(2, status)
        self.assertEqual(3, len(self.store.read("paused")["events"]))
        self.assertNotIn("Storage recollection", out.getvalue())

    def test_phase_claim_is_exclusive(self):
        self.start()
        self.store.claim("paused", "reviewing")
        with self.assertRaisesRegex(ValueError, "reviewing"):
            ProofStore(Path(self.temp.name), "test-001").claim("paused", "reviewing")

    def test_rejects_unsafe_run_ids(self):
        for run_id in ("../escape", "a/b", "", "x" * 65):
            with self.assertRaises(ValueError):
                ProofStore(Path(self.temp.name), run_id)


if __name__ == "__main__":
    unittest.main()
