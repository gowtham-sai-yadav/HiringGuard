"""Member C — RiskScorer + validator tests. All offline (LLM mocked)."""
from __future__ import annotations

import pytest

from hireguard.agents import risk as risk_mod
from hireguard.agents.risk import risk_node
from hireguard.state import Finding, PipelineState, ScoredFinding, load_packet
from hireguard.tools.validators import (
    SEVERITY_BANDS,
    known_rule_ids,
    validate_evidence_quote_in_packet,
    validate_rule_id_exists,
    validate_severity_score_consistent,
)


@pytest.fixture
def acme_packet():
    return load_packet("hireguard/samples/acme_se_role.json")


def _finding(rule_id="IND-AGE-BAR", quote="young, energetic software engineer", quality=0.9):
    return Finding(
        rule_id=rule_id,
        citation="Constitution of India, Art. 14",
        evidence_quote=quote,
        evidence_quality=quality,
        rationale="age-coded language in posting",
    )


def _scored(finding, severity="critical", exposure=85):
    return ScoredFinding(
        finding=finding,
        severity=severity,
        likelihood=0.8,
        jurisdiction_attaches=True,
        exposure_score=exposure,
        scorer_rationale="test",
    )


# ── validator: rule_id existence ────────────────────────────────────────────


def test_rule_id_validator_accepts_known():
    assert validate_rule_id_exists("IND-AGE-BAR")
    assert "IND-PAY-PARITY" in known_rule_ids()


def test_rule_id_validator_rejects_unknown():
    assert not validate_rule_id_exists("STUB-RULE")
    assert not validate_rule_id_exists("TOTALLY-MADE-UP")


# ── validator: severity ↔ exposure band ─────────────────────────────────────


def test_critical_finding_scores_above_75():
    assert SEVERITY_BANDS["critical"] == (75, 100)
    assert validate_severity_score_consistent(_scored(_finding(), "critical", 90))
    assert not validate_severity_score_consistent(_scored(_finding(), "critical", 60))


def test_severity_score_band_validator_rejects_misaligned():
    # "low" severity but a high exposure score → inconsistent.
    assert not validate_severity_score_consistent(_scored(_finding(), "low", 80))
    # aligned low.
    assert validate_severity_score_consistent(_scored(_finding(), "low", 10))


# ── validator: evidence quote presence ──────────────────────────────────────


def test_evidence_quote_found_in_packet(acme_packet):
    f = _finding(quote="young, energetic")
    assert validate_evidence_quote_in_packet(f, acme_packet)


def test_evidence_quote_from_scorecard_note(acme_packet):
    f = _finding(rule_id="IND-SUBJECTIVE-CRITERIA", quote="feel like one of us")
    assert validate_evidence_quote_in_packet(f, acme_packet)


def test_fabricated_evidence_quote_rejected(acme_packet):
    f = _finding(quote="we only hire astronauts from mars")
    assert not validate_evidence_quote_in_packet(f, acme_packet)


# ── risk_node integration (LLM mocked) ──────────────────────────────────────


def _patch_scorer(monkeypatch, severity="critical", exposure=85):
    async def fake_score(finding, jurisdiction):
        return _scored(finding, severity, exposure)

    monkeypatch.setattr(risk_mod, "score_finding", fake_score)


@pytest.mark.asyncio
async def test_risk_node_scores_every_finding(monkeypatch, acme_packet):
    _patch_scorer(monkeypatch)
    findings = [_finding(), _finding(rule_id="IND-PAY-PARITY", quote="current salary")]
    state = PipelineState(packet=acme_packet, findings=findings)

    out = await risk_node(state)

    assert len(out["scored_findings"]) == 2
    assert all(isinstance(s, ScoredFinding) for s in out["scored_findings"])


@pytest.mark.asyncio
async def test_unknown_rule_id_triggers_human_review(monkeypatch, acme_packet):
    _patch_scorer(monkeypatch)
    state = PipelineState(
        packet=acme_packet,
        findings=[_finding(rule_id="STUB-RULE", quote="young, energetic")],
    )
    out = await risk_node(state)
    sf = out["scored_findings"][0]
    assert sf.needs_human_review
    assert "unknown rule_id" in sf.scorer_rationale


@pytest.mark.asyncio
async def test_low_evidence_quality_triggers_human_review(monkeypatch, acme_packet):
    _patch_scorer(monkeypatch)
    state = PipelineState(
        packet=acme_packet,
        findings=[_finding(quote="young, energetic", quality=0.2)],
    )
    out = await risk_node(state)
    sf = out["scored_findings"][0]
    assert sf.needs_human_review
    assert "low evidence_quality" in sf.scorer_rationale


@pytest.mark.asyncio
async def test_band_mismatch_triggers_human_review(monkeypatch, acme_packet):
    # Scorer returns an inconsistent severity/exposure pair → guardrail flags it.
    _patch_scorer(monkeypatch, severity="low", exposure=95)
    state = PipelineState(packet=acme_packet, findings=[_finding(quote="young, energetic")])
    out = await risk_node(state)
    sf = out["scored_findings"][0]
    assert sf.needs_human_review
    assert "inconsistent" in sf.scorer_rationale


@pytest.mark.asyncio
async def test_clean_finding_not_flagged(monkeypatch, acme_packet):
    _patch_scorer(monkeypatch, severity="critical", exposure=85)
    state = PipelineState(packet=acme_packet, findings=[_finding(quote="young, energetic")])
    out = await risk_node(state)
    sf = out["scored_findings"][0]
    assert not sf.needs_human_review


@pytest.mark.asyncio
async def test_scoring_failure_degrades_to_human_review(monkeypatch, acme_packet):
    async def boom(finding, jurisdiction):
        raise RuntimeError("groq down")

    monkeypatch.setattr(risk_mod, "score_finding", boom)
    state = PipelineState(packet=acme_packet, findings=[_finding(quote="young, energetic")])
    out = await risk_node(state)
    sf = out["scored_findings"][0]
    assert sf.needs_human_review
    assert any("scoring failed" in e for e in out["errors"])
