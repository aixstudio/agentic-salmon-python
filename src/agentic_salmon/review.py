"""Review agent: convert explicit human judgment into a bounded gate decision."""

from __future__ import annotations

from .models import (
    GateDecision,
    HumanEvidence,
    HypothesisStatus,
    ReasonResult,
    ReviewResult,
)


class ReviewAgent:
    def review(self, reason: ReasonResult, evidence: HumanEvidence) -> ReviewResult:
        retained, resolved = _reconcile_unknowns(reason, evidence)
        if evidence.concern_reported:
            return ReviewResult(
                decision=GateDecision.STOP,
                reasons=("The human reported a concern; bounded guidance is blocked.",),
                retained_unknowns=retained,
                selected_hypothesis=evidence.selected_hypothesis,
                resolved_unknowns=resolved,
            )
        if evidence.authorizes_bounded_guidance is False:
            return ReviewResult(
                decision=GateDecision.STOP,
                reasons=("The human declined bounded guidance.",),
                retained_unknowns=retained,
                selected_hypothesis=evidence.selected_hypothesis,
                resolved_unknowns=resolved,
            )

        missing: list[str] = []
        hypotheses_by_id = {
            hypothesis.hypothesis_id: hypothesis for hypothesis in reason.hypotheses
        }
        if (
            evidence.selected_hypothesis
            and evidence.selected_hypothesis not in hypotheses_by_id
        ):
            return ReviewResult(
                decision=GateDecision.ASK,
                reasons=(
                    "Review requires: hypothesis must be one of: "
                    + ", ".join(sorted(hypotheses_by_id)),
                ),
                retained_unknowns=retained,
                selected_hypothesis=evidence.selected_hypothesis,
                resolved_unknowns=resolved,
            )
        if evidence.selected_hypothesis:
            selected = hypotheses_by_id[evidence.selected_hypothesis]
            if selected.status is HypothesisStatus.UNKNOWN:
                return ReviewResult(
                    decision=GateDecision.ASK,
                    reasons=(
                        "Review cannot authorize action for an unknown hypothesis; "
                        "select a supported or plausible hypothesis, or stop.",
                    ),
                    retained_unknowns=retained,
                    selected_hypothesis=evidence.selected_hypothesis,
                    resolved_unknowns=resolved,
                )
        if not evidence.selected_hypothesis:
            missing.append("human-selected working hypothesis")
        if evidence.accepts_retained_unknowns is not True:
            missing.append("explicit acceptance of retained uncertainty")
        if evidence.authorizes_bounded_guidance is not True:
            missing.append("explicit authorization for bounded guidance")
        if missing:
            return ReviewResult(
                decision=GateDecision.ASK,
                reasons=("Review requires: " + "; ".join(missing),),
                retained_unknowns=retained,
                selected_hypothesis=evidence.selected_hypothesis,
                resolved_unknowns=resolved,
            )

        return ReviewResult(
            decision=GateDecision.ALLOW_BOUNDED,
            reasons=(
                "The human selected a working hypothesis, retained the unknowns, "
                "and authorized only bounded cited guidance.",
            ),
            retained_unknowns=retained,
            selected_hypothesis=evidence.selected_hypothesis,
            resolved_unknowns=resolved,
        )


def _reconcile_unknowns(
    reason: ReasonResult,
    evidence: HumanEvidence,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    retained = list(reason.retained_unknowns)
    resolved: list[str] = []
    # Free-text recollections are reported evidence, not verified resolutions.
    # The deterministic proof supplies no new storage or odor observations.
    return tuple(retained), tuple(resolved)
