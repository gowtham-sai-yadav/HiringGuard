"""Output guardrails — Member C.

Three deterministic checks that run AFTER the LLM scores a finding. They are the
"the AI cannot lie" layer of the product:

  1. validate_rule_id_exists       — no hallucinated statutes.
  2. validate_severity_score_consistent — severity label must match the 0-100
                                     exposure band (critical→75-100, etc.).
  3. validate_evidence_quote_in_packet  — the quoted evidence must actually be
                                     present in the uploaded packet text.

None of these raise. A failure is a SIGNAL: the RiskScorer node sets
`needs_human_review=True` on the finding rather than crashing the audit. The
graph always reaches the human gate.

The canonical rule list is `hireguard/rag/ruleset.json` — the same file Member B
seeds into Supabase. Validating against it means these guardrails work whether or
not the database is up.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from hireguard.state import Finding, HiringPacket, ScoredFinding

_RULESET_PATH = Path(__file__).resolve().parent.parent / "rag" / "ruleset.json"

# severity label → inclusive (min, max) exposure_score band.
SEVERITY_BANDS: dict[str, tuple[int, int]] = {
    "low": (0, 24),
    "medium": (25, 49),
    "high": (50, 74),
    "critical": (75, 100),
}


@lru_cache(maxsize=1)
def known_rule_ids() -> frozenset[str]:
    """Rule IDs that legally exist. Loaded once from the canonical ruleset."""
    raw = json.loads(_RULESET_PATH.read_text())
    return frozenset(r["rule_id"] for r in raw.get("rules", []))


def validate_rule_id_exists(rule_id: str) -> bool:
    """Guardrail #1 — reject rule_ids the system invented."""
    return rule_id in known_rule_ids()


def validate_severity_score_consistent(sf: ScoredFinding) -> bool:
    """Guardrail #2 — severity label must agree with the exposure band."""
    band = SEVERITY_BANDS.get(sf.severity)
    if band is None:
        return False
    lo, hi = band
    return lo <= sf.exposure_score <= hi


def _normalize(text: str) -> str:
    """Lowercase + collapse all whitespace so quote matching is robust to
    formatting differences (newlines, double spaces) but nothing else."""
    return " ".join(text.lower().split())


def _packet_haystack(packet: HiringPacket) -> str:
    """All recruiter-supplied free text the evidence could legitimately come
    from: the posting blob plus scorecard criteria names/notes."""
    parts: list[str] = [packet.job_posting or "", packet.interview_scorecard.title]
    for c in packet.interview_scorecard.criteria:
        parts.append(c.name)
        if c.note:
            parts.append(c.note)
    return _normalize(" ".join(parts))


def validate_evidence_quote_in_packet(finding: Finding, packet: HiringPacket) -> bool:
    """Guardrail #3 — the evidence_quote must appear in the packet text.

    Note: absence-based findings (e.g. "no salary range posted") have no literal
    quote to anchor; those legitimately fail here and get routed to a human,
    which is the correct conservative behavior.
    """
    quote = _normalize(finding.evidence_quote)
    if not quote:
        return False
    return quote in _packet_haystack(packet)
