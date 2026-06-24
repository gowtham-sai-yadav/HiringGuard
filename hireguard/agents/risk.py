"""RiskScorer — Member C's node.

For each Finding from Policy:
  1. Score it (severity + likelihood + exposure_score + jurisdiction_attaches)
     via structured output (Groq/Llama, Claude-Haiku fallback).
  2. Run three output guardrails: rule_id exists, severity/exposure band aligned,
     evidence quote present in the packet.
  3. Low evidence_quality OR any failed guardrail → needs_human_review=True
     (never crash — the human gate downstream is the safety net).
  4. Return {"scored_findings": [...]}.

Signature is fixed by Member A's graph wiring — do not change it.
"""
from __future__ import annotations

import logging

from hireguard.state import Finding, PipelineState, ScoredFinding
from hireguard.tools.score_exposure import score_finding
from hireguard.tools.validators import (
    validate_evidence_quote_in_packet,
    validate_rule_id_exists,
    validate_severity_score_consistent,
)

log = logging.getLogger(__name__)

# Below this Policy-assigned evidence quality, a human must confirm the finding.
EVIDENCE_QUALITY_FLOOR = 0.5


def _fallback_score(finding: Finding, reason: str) -> ScoredFinding:
    """Used only if the scoring LLM call itself fails. Conservative + flagged."""
    return ScoredFinding(
        finding=finding,
        severity="medium",
        likelihood=0.5,
        jurisdiction_attaches=True,
        exposure_score=35,
        scorer_rationale=f"Automated scoring unavailable ({reason}); routed to human.",
        needs_human_review=True,
    )


def _apply_guardrails(sf: ScoredFinding, state: PipelineState) -> tuple[ScoredFinding, list[str]]:
    """Run the three validators + the evidence-quality floor. Returns the
    (possibly flagged) ScoredFinding and a list of reasons it was flagged."""
    finding = sf.finding
    reasons: list[str] = []

    if not validate_rule_id_exists(finding.rule_id):
        reasons.append(f"unknown rule_id '{finding.rule_id}'")
    if not validate_severity_score_consistent(sf):
        reasons.append(
            f"severity '{sf.severity}' inconsistent with exposure {sf.exposure_score}"
        )
    if not validate_evidence_quote_in_packet(finding, state.packet):
        reasons.append("evidence quote not found in packet")
    if finding.evidence_quality < EVIDENCE_QUALITY_FLOOR:
        reasons.append(f"low evidence_quality ({finding.evidence_quality})")

    if reasons:
        sf = sf.model_copy(
            update={
                "needs_human_review": True,
                "scorer_rationale": f"{sf.scorer_rationale} [flagged: {'; '.join(reasons)}]",
            }
        )
    return sf, reasons


async def risk_node(state: PipelineState) -> dict:
    jurisdiction = (
        state.facts.jurisdiction if state.facts else state.packet.primary_work_location
    )
    log.info("risk_node: scoring %d finding(s) for %s", len(state.findings), jurisdiction)

    scored: list[ScoredFinding] = []
    errors: list[str] = []

    for finding in state.findings:
        try:
            sf = await score_finding(finding, jurisdiction)
        except Exception as exc:  # noqa: BLE001 — degrade, never break the audit
            log.warning("risk_node: scoring failed for %s: %s", finding.rule_id, exc)
            sf = _fallback_score(finding, str(exc))
            errors.append(f"[risk_node] scoring failed for {finding.rule_id}: {exc}")

        sf, reasons = _apply_guardrails(sf, state)
        if reasons:
            errors.append(f"[risk_node] {finding.rule_id} → human review: {reasons}")
        scored.append(sf)

    flagged = sum(1 for s in scored if s.needs_human_review)
    log.info("risk_node done: %d scored, %d flagged for human review", len(scored), flagged)

    return {"scored_findings": scored, "errors": errors}
