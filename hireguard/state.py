"""
HireGuard v2 — shared state contract.

EVERY agent imports from this file. EVERY handoff between nodes is a Pydantic
model defined here. Do NOT modify this file without pinging Member A in chat —
schema drift breaks the graph for everyone.

The HiringPacket shape matches the JSON files in `hireguard/samples/` (which
are copied verbatim from the HireGuard base repo — the moat). If you need a
new field:
  1. Post in chat.
  2. Add it here.
  3. Bump the version comment below.
  4. Tell teammates to `git pull` before continuing.

Version: 2  (matches HireGuard base sample packet shape)
"""
from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, Any, Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


# ──────────────────────────────────────────────────────────────────────────────
# INPUTS — what the recruiter uploads. Shape matches hireguard/samples/*.json
# ──────────────────────────────────────────────────────────────────────────────


class CompBand(BaseModel):
    """Compensation band — public + internal. Mirrors base packet shape."""

    currency: str = "USD"
    posted_range_in_listing: Optional[Any] = None  # null | str | [min, max]
    internal_band_min: Optional[int] = None
    internal_band_max: Optional[int] = None
    benefits_described_in_listing: bool = False


class ScorecardCriterion(BaseModel):
    name: str
    scale: str = "1-5"
    anchored: bool = True
    note: Optional[str] = None


class InterviewScorecard(BaseModel):
    title: str = "Interview Rubric"
    criteria: list[ScorecardCriterion] = []


class HiringPacket(BaseModel):
    """Top-level input. Field names match HireGuard base samples verbatim."""

    model_config = ConfigDict(populate_by_name=True)

    packet_id: str
    company: str
    company_size: Optional[int] = None
    primary_work_location: str  # e.g. "San Francisco, CA", "Remote-US"
    job_posting: str  # full posting text blob — Policy reads this directly
    comp_band: CompBand
    interview_scorecard: InterviewScorecard

    # Gold-label for evals only. Agents MUST NOT read this.
    # Aliased so the base JSON's `_planted_violations_for_demo` loads cleanly.
    planted_violations_for_demo: Optional[list[str]] = Field(
        default=None, alias="_planted_violations_for_demo"
    )


# ──────────────────────────────────────────────────────────────────────────────
# INTERMEDIATE HANDOFFS — outputs of each node
# ──────────────────────────────────────────────────────────────────────────────


class IntakeFacts(BaseModel):
    """Output of IntakeAgent (Member A).

    Schema is Indian-law-aware (project pivoted to Indian compliance on
    2026-06-24). The legacy US-only boolean fields are kept with default-False
    so older code paths and tests keep working — they are simply unused in the
    Indian context.

    All `*_phrases` / `*_signals` lists carry EXACT quotes lifted from the
    packet so the Policy agent can cite them as evidence — never paraphrase.
    """

    # Jurisdiction code. India: state codes (e.g. 'KA', 'MH', 'DL', 'TN', 'KL',
    # 'GJ', 'TG', 'UP', 'WB', 'RJ', 'PB', 'AP'); pan-India / Union law: 'India-Central';
    # remote-India: 'India-Remote'; non-Indian: 'INTERNATIONAL'; unclear: 'UNKNOWN'.
    jurisdiction: str

    # Universal data points (apply in any jurisdiction).
    pay_range_disclosed: bool
    benefits_disclosed: bool
    scorecard_question_count: int
    subjective_scorecard_criteria: list[str] = []
    age_coded_phrases: list[str] = []  # IND-AGE-BAR (weak) + general litigation risk

    # ── Indian-law signals — these are the rule-mapped fields the Policy node uses ─
    # Each list holds exact phrases lifted verbatim from the packet (max ~10 each).
    gender_restrictive_phrases: list[str] = []          # IND-GENDER-CODED + IND-TRANSGENDER
    caste_or_community_signals: list[str] = []          # IND-CASTE-RELIGION
    marital_or_pregnancy_signals: list[str] = []        # IND-MATERNITY-MARITAL
    medical_or_hiv_test_signals: list[str] = []         # IND-HIV-MEDICAL
    non_essential_physical_requirements: list[str] = [] # IND-DISABILITY-RPWD
    domicile_or_language_restriction: list[str] = []    # IND-DOMICILE-LANGUAGE

    # ── Deprecated US-only fields (kept for back-compat; unused under Indian law) ──
    # Indian law has no direct equivalent of US salary-history bans or ban-the-box.
    # Keep these so the older intake prompt and tests still load; default to False
    # in the Indian flow.
    salary_history_question_present: bool = False  # deprecated (US-only)
    criminal_history_question_present: bool = False  # deprecated (US-only)

    # Set by the input-validator / PII guard, not by the LLM.
    pii_redacted_labels: list[str] = []
    injection_attempt_detected: bool = False
    notes: str = ""


class Finding(BaseModel):
    """Output of PolicyAgent (Member B), per detected violation."""

    finding_id: str = Field(default_factory=lambda: uuid4().hex[:8])
    rule_id: str  # MUST exist in the rules table (validated by C)
    citation: str
    evidence_quote: str = Field(max_length=500)
    evidence_quality: float = Field(ge=0.0, le=1.0)
    rationale: str


class ScoredFinding(BaseModel):
    """Output of RiskScorer (Member C). Wraps a Finding with scores."""

    finding: Finding
    severity: Literal["low", "medium", "high", "critical"]
    likelihood: float = Field(ge=0.0, le=1.0)
    jurisdiction_attaches: bool
    exposure_score: int = Field(ge=0, le=100)
    scorer_rationale: str
    needs_human_review: bool = False


class RecommendedFix(BaseModel):
    finding_id: str
    fix_text: str
    priority: Literal["must_fix", "should_fix", "nice_to_fix"]


class AuditMemo(BaseModel):
    """Output of CounselAgent (Member D). The final deliverable."""

    memo_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    executive_summary: str
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    scored_findings: list[ScoredFinding] = []
    recommended_fixes: list[RecommendedFix] = []
    needs_re_review: bool = False
    re_review_reason: Optional[str] = None


class HumanApproval(BaseModel):
    """Payload returned by the HITL gate via Command(resume=...)."""

    decision: Literal["approve", "reject", "send_back"]
    reviewer_note: str = ""
    reviewed_at: datetime = Field(default_factory=datetime.utcnow)


# ──────────────────────────────────────────────────────────────────────────────
# GRAPH STATE — shared across every node
# ──────────────────────────────────────────────────────────────────────────────


class PipelineState(BaseModel):
    """Shared LangGraph state. Validated at every node boundary."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: UUID = Field(default_factory=uuid4)
    packet: HiringPacket
    facts: Optional[IntakeFacts] = None
    findings: list[Finding] = []
    scored_findings: list[ScoredFinding] = []
    audit_memo: Optional[AuditMemo] = None
    human_approval: Optional[HumanApproval] = None
    revision_count: int = 0
    # Reducer: appended-to by every node that emits a non-fatal error/note.
    errors: Annotated[list[str], operator.add] = []


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────


def load_packet(path: str) -> HiringPacket:
    """Load a sample packet JSON from disk → validated HiringPacket."""
    import json
    from pathlib import Path

    raw = json.loads(Path(path).read_text())
    return HiringPacket.model_validate(raw)
