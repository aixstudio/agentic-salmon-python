"""Three deterministic, durable proofs of human authority."""
from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path
from .proofs import ProofStore, perform, review_packet, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Proof-1, Proof-2, then Proof-3 with the same run ID.")
    parser.add_argument("command", choices=("proof-1", "proof-2", "proof-3"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--log-directory", type=Path, default=Path("log"))
    args = parser.parse_args(argv)
    try:
        store = ProofStore(args.log_directory, args.run_id)
        decision = None
        if args.command == "proof-2":
            print(review_packet(store.read("paused")))
            try:
                decision = input("Type APPROVE to accept this contract, or STOP/Enter to end: ").strip().upper()
            except EOFError:
                decision = "STOP"
            if decision not in ("APPROVE", "STOP", ""):
                print("Input not accepted. Run remains paused; rerun Proof-2 and enter APPROVE or STOP.", file=sys.stderr)
                return 2
        result = asyncio.run(perform(args.command, store, decision))
        print(summary(args.command, result, store, ".venv/bin/python -m agentic_salmon"))
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        print(f"Proof blocked: {error}", file=sys.stderr)
        return 1
