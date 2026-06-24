"""Exposure scoring — Member C.

`score_finding` turns one Policy `Finding` into a fully-scored `ScoredFinding`
using structured output (no JSON-string parsing).

Design note: the LLM only produces the *scoring* fields (`RiskAssessment`). We
attach the original `Finding` ourselves, so the model can never corrupt or
re-paraphrase the upstream evidence — the handoff stays byte-for-byte intact.

LLM: `get_groq()` (Llama-3.3-70B). When no GROQ_API_KEY is set, the factory
transparently falls back to Claude Haiku, so this works on a Claude-only setup.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from hireguard.llm import get_groq
from hireguard.state import Finding, ScoredFinding

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "risk.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text()


class RiskAssessment(BaseModel):
    """Scoring-only fields the LLM returns. Bands enforced downstream by
    `validate_severity_score_consistent`."""

    severity: Literal["low", "medium", "high", "critical"]
    likelihood: float = Field(ge=0.0, le=1.0)
    jurisdiction_attaches: bool
    exposure_score: int = Field(ge=0, le=100)
    scorer_rationale: str


def _finding_to_message(finding: Finding, jurisdiction: str) -> str:
    return (
        f"# FINDING TO SCORE\n\n"
        f"Work location / jurisdiction: {jurisdiction}\n"
        f"rule_id: {finding.rule_id}\n"
        f"citation: {finding.citation}\n"
        f"evidence_quote: {finding.evidence_quote!r}\n"
        f"evidence_quality (0-1): {finding.evidence_quality}\n"
        f"policy_rationale: {finding.rationale}\n"
    )


async def score_finding(finding: Finding, jurisdiction: str) -> ScoredFinding:
    """Score a single finding. Returns a validated ScoredFinding.

    Raises only if the LLM call itself fails — the RiskScorer node catches that
    and degrades gracefully to a human-review flag.
    """
    llm = get_groq(temperature=0.0).with_structured_output(RiskAssessment)
    assessment: RiskAssessment = await llm.ainvoke(
        [
            ("system", SYSTEM_PROMPT),
            ("user", _finding_to_message(finding, jurisdiction)),
        ]
    )
    return ScoredFinding(
        finding=finding,
        needs_human_review=False,
        **assessment.model_dump(),
    )
