"""Durable demo checkpoints and bounded terminal guidance. SQLite is trusted local state."""
from __future__ import annotations
import json
import os
import re
import sqlite3
import sys
import traceback
from contextlib import contextmanager, redirect_stderr
from datetime import UTC, datetime
from pathlib import Path
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from .knowledge_port import McpKnowledgePort
from .models import HumanEvidence, RunResult
from .perceive import PerceiveAgent
from .providers import ReviewedFixtureProvider
from .reason import ReasonAgent
from .workflow import AgenticSalmonWorkflow

OUTCOME = "Historical case record: 390°F for 14 minutes; wonderful and fresh-tasting. Not a new cooking trial or proof of safety."
FEEDBACK = "Historical case preference: moderately spicy result preferred."


@contextmanager
def _database(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


class ProofStore:
    def __init__(self, directory: Path, run_id: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
            raise ValueError("run ID must contain 1–64 letters, digits, underscores or hyphens")
        self.directory, self.run_id = directory, run_id
        self.database = directory / f"{run_id}.sqlite3"
        self.log = directory / f"{run_id}.log"

    def reserve(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ValueError("run ID already exists; continue its next proof or use a new run ID") from exc
        os.close(fd)
        with _database(self.database) as db:
            db.execute("CREATE TABLE proof_run (id INTEGER PRIMARY KEY CHECK(id=1), phase TEXT NOT NULL, result TEXT)")
            db.execute("INSERT INTO proof_run VALUES (1, 'starting', NULL)")

    def read(self, expected: str) -> dict:
        if not self.database.is_file():
            raise ValueError("unknown run ID; run Proof-1 first")
        with _database(self.database) as db:
            try:
                row = db.execute("SELECT phase, result FROM proof_run WHERE id=1").fetchone()
            except sqlite3.DatabaseError as exc:
                raise ValueError("checkpoint is not a supported proof database") from exc
        if not row or row[0] != expected:
            raise ValueError(f"expected {expected}, found {row[0] if row else 'missing'}; no stage was rerun")
        return json.loads(row[1])

    def claim(self, expected: str, active: str) -> None:
        self.read(expected)
        with _database(self.database) as db:
            changed = db.execute("UPDATE proof_run SET phase=? WHERE id=1 AND phase=?", (active, expected)).rowcount
        if changed != 1:
            raise ValueError("another process already claimed this proof; no stage was rerun")

    def finish(self, phase: str, result: RunResult) -> None:
        payload = result.to_dict()
        with _database(self.database) as db:
            db.execute("UPDATE proof_run SET phase=?, result=? WHERE id=1", (phase, json.dumps(payload)))
        self.record({"at": datetime.now(UTC).isoformat(), "phase": phase, "result": payload})

    def record(self, value: dict) -> None:
        fd = os.open(self.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as output:
            output.write(json.dumps(value, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())


async def perform(command: str, store: ProofStore, decision: str | None = None, *, provider=None, knowledge=None) -> RunResult:
    if command == "proof-1":
        store.reserve()
    elif command == "proof-2":
        if decision not in ("APPROVE", "STOP", ""):
            raise ValueError("review requires explicit APPROVE or STOP")
        store.claim("paused", "reviewing")
    elif command == "proof-3":
        store.claim("adjudicated", "executing")
    else:
        raise ValueError("unknown proof")
    try:
        fd = os.open(store.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as details, redirect_stderr(details):
            async with AsyncSqliteSaver.from_conn_string(str(store.database)) as saver:
                workflow = AgenticSalmonWorkflow(
                    PerceiveAgent(provider or ReviewedFixtureProvider(Path("examples/frozen-salmon-observation.json"), Path("docs/assets/salmon-pre-cook-input.jpeg"))),
                    ReasonAgent(knowledge or McpKnowledgePort(python_executable=sys.executable, working_directory=Path.cwd())),
                    checkpointer=saver, defer_action=True,
                )
                if command == "proof-1":
                    result = await workflow.astart(run_id=store.run_id, outcome=OUTCOME, feedback=FEEDBACK)
                    phase = "paused"
                elif command == "proof-2":
                    approved = decision == "APPROVE"
                    result = await workflow.aresume(store.run_id, HumanEvidence(
                        selected_hypothesis="fish" if approved else None,
                        accepts_retained_unknowns=approved, authorizes_bounded_guidance=approved,
                    ))
                    phase = "adjudicated" if result.status == "adjudicated" else "stopped"
                else:
                    result = await workflow.aexecute(store.run_id)
                    phase = "completed"
        expected = {"proof-1": ("interrupted", 3), "proof-2": ("adjudicated", 4) if decision == "APPROVE" else ("stopped", 4), "proof-3": ("completed", 6)}[command]
        if (result.status, len(result.events)) != expected:
            raise RuntimeError("proof contract failed")
        store.finish(phase, result)
        return result
    except Exception:
        store.record({"phase": "failed", "error": traceback.format_exc()})
        with _database(store.database) as db:
            db.execute("UPDATE proof_run SET phase='failed' WHERE id=1")
        raise RuntimeError(f"execution failed; details: {store.log}") from None


def review_packet(result: dict) -> str:
    reason = result["events"][2]["evidence"]
    offered = {h["hypothesis_id"]: h for h in reason["hypotheses"]}
    if offered.get("fish", {}).get("status") != "supported":
        raise ValueError("saved evidence does not support the deterministic fish review contract")
    return (
        f"HITL review — run {result['run_id']} (saved Proof-1 evidence)\n"
        "Review: visible portions and skin-like surface support fish as a working hypothesis; species is unverified.\n"
        f"Retain ALL unknowns: {'; '.join(reason['retained_unknowns'])}.\n"
        "Authority requested: bounded thermometer guidance with a retrieved citation; no safety or identity certification.\n"
        "APPROVE selects fish, accepts these unknowns, and authorizes only that guidance.\n"
        "Watch out: do not supply guessed observations. STOP/Enter ends without action."
    )


def summary(command: str, result: RunResult, store: ProofStore, launcher: str) -> str:
    lines = [f"{command.upper()} | run {result.run_id} | {result.status.upper()}"]
    if command == "proof-1":
        lines += ["Perceive → Connect → Reason completed; real RAG/MCP evidence saved.",
                  *review_packet(result.to_dict()).splitlines()[1:4],
                  "Watch out: retrieved knowledge is not permission. Act/Learn have NOT run."]
        next_proof = "proof-2"
    elif result.status == "stopped":
        lines += ["Human declined. Act/Learn have NOT run.", "TEST END — STOPPED. No further proof may execute this run."]
        next_proof = None
    elif command == "proof-2":
        lines += ["HITL adjudication saved: ALLOW_BOUNDED; all unknowns retained.", "Watch out: approval is recorded; Act/Learn have NOT run."]
        next_proof = "proof-3"
    else:
        lines += ["Act released human-authorized, cited guidance; Learn recorded the fixed historical case outcome.",
                  "Watch out: identity/safety remain unverified; historical outcome changes no policy.",
                  "TEST END — PASS. All six agents executed once; no further action required."]
        next_proof = None
    if next_proof:
        lines.append(f"Next: {launcher} {next_proof} --run-id {result.run_id}" + (f" --log-directory {store.directory}" if store.directory != Path("log") else ""))
    lines.append(f"Details: {store.log}")
    return "\n".join(lines)
