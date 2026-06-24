"""Schema contract tests. Run first; if these fail, nothing downstream works."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from hireguard.state import (
    AuditMemo,
    Finding,
    HiringPacket,
    HumanApproval,
    IntakeFacts,
    PipelineState,
    ScoredFinding,
    load_packet,
)


# ─── Sample-packet loading (both base files must parse) ──────────────────────


def test_load_acme_sample():
    p = load_packet("hireguard/samples/acme_se_role.json")
    assert isinstance(p, HiringPacket)
    assert p.packet_id == "acme_se_role"
    # Pivoted to Indian context on 2026-06-24
    assert p.primary_work_location == "Bengaluru, Karnataka"
    assert p.comp_band.currency == "INR"
    assert len(p.interview_scorecard.criteria) == 4
    # 7 planted violations spanning gender / caste / marital / RPwD / HIV / age / subjective
    assert len(p.planted_violations_for_demo or []) == 7
    assert all(v.startswith("IND-") for v in (p.planted_violations_for_demo or []))


def test_load_northwind_sample():
    p = load_packet("hireguard/samples/northwind_pm_role.json")
    assert isinstance(p, HiringPacket)
    assert p.packet_id == "northwind_pm_role"


# ─── Field-level validation ──────────────────────────────────────────────────


def test_finding_evidence_quote_max_length():
    with pytest.raises(ValidationError):
        Finding(
            rule_id="X",
            citation="cite",
            evidence_quote="x" * 600,
            evidence_quality=0.5,
            rationale="r",
        )


def test_finding_evidence_quality_range():
    base = dict(rule_id="X", citation="c", evidence_quote="q", rationale="r")
    with pytest.raises(ValidationError):
        Finding(**base, evidence_quality=1.5)
    with pytest.raises(ValidationError):
        Finding(**base, evidence_quality=-0.1)


def test_scored_finding_exposure_score_range():
    f = Finding(rule_id="X", citation="c", evidence_quote="q",
                evidence_quality=0.7, rationale="r")
    with pytest.raises(ValidationError):
        ScoredFinding(
            finding=f, severity="high", likelihood=0.5,
            jurisdiction_attaches=True, exposure_score=150,
            scorer_rationale="r",
        )


def test_scored_finding_severity_literal():
    f = Finding(rule_id="X", citation="c", evidence_quote="q",
                evidence_quality=0.7, rationale="r")
    with pytest.raises(ValidationError):
        ScoredFinding(
            finding=f, severity="EXTREMELY_BAD", likelihood=0.5,  # type: ignore
            jurisdiction_attaches=True, exposure_score=50,
            scorer_rationale="r",
        )


def test_human_approval_decision_literal():
    with pytest.raises(ValidationError):
        HumanApproval(decision="maybe")  # type: ignore


def test_state_round_trips_via_json():
    p = load_packet("hireguard/samples/acme_se_role.json")
    s = PipelineState(packet=p)
    j = s.model_dump_json()
    s2 = PipelineState.model_validate_json(j)
    assert s2.packet.packet_id == s.packet.packet_id


def test_intake_facts_defaults():
    f = IntakeFacts(
        jurisdiction="CA",
        pay_range_disclosed=False,
        benefits_disclosed=False,
        salary_history_question_present=False,
        criminal_history_question_present=False,
        scorecard_question_count=0,
    )
    assert f.age_coded_phrases == []
    assert f.pii_redacted_labels == []
    assert f.injection_attempt_detected is False


def test_audit_memo_defaults_counts_to_zero():
    m = AuditMemo(executive_summary="s")
    assert m.critical_count == 0
    assert m.scored_findings == []
