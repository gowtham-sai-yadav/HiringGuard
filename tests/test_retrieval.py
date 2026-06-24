"""Member B — retrieval / RAG evals (INDIAN ruleset).

All pinned to backend="local" (the deterministic LocalRetriever) so they run in
CI with no Supabase and no API keys. The Supabase path shares the same
jurisdiction filter, so these invariants hold there too.
"""
from __future__ import annotations

from hireguard.tools.retrieve_rules import (
    CENTRAL,
    normalize_role_jurisdiction,
    retrieve_rules,
)

ALL_RULES = {
    "IND-GENDER-CODED", "IND-PAY-PARITY", "IND-CASTE-RELIGION",
    "IND-MATERNITY-MARITAL", "IND-DISABILITY-RPWD", "IND-TRANSGENDER",
    "IND-HIV-MEDICAL", "IND-AGE-BAR", "IND-DOMICILE-LANGUAGE",
    "IND-SUBJECTIVE-CRITERIA",
}


def _ids(rules):
    return {r["rule_id"] for r in rules}


# ── 1. Jurisdiction filtering ────────────────────────────────────────────────


def test_central_rules_apply_to_any_indian_role():
    """India's equality law is Union/Central → every role sees the central rules,
    whatever the city/state."""
    for loc in ("Bengaluru, KA", "Mumbai, MH", "KA", "Remote-India"):
        ids = _ids(retrieve_rules("hiring compliance review", loc, k=10, backend="local"))
        assert ALL_RULES <= ids, f"{loc} missing rules: {ALL_RULES - ids}"


def test_unknown_jurisdiction_returns_central_not_empty():
    """A garbage location degrades to the central rules — never empty."""
    rules = retrieve_rules("review", "Atlantis-9000", k=10, backend="local")
    assert rules, "must not return empty — central law always applies"
    assert all(CENTRAL in r["jurisdiction_codes"] for r in rules)


# ── 2. Semantic relevance ────────────────────────────────────────────────────


def test_top_k_relevance_caste():
    rules = retrieve_rules(
        "we want only brahmin upper-caste candidates from the same community",
        "KA", k=10, backend="local",
    )
    assert "IND-CASTE-RELIGION" in [r["rule_id"] for r in rules[:3]]


def test_top_k_relevance_maternity():
    rules = retrieve_rules(
        "applicant must not be pregnant; no marriage plans; married women need not apply",
        "MH", k=10, backend="local",
    )
    assert "IND-MATERNITY-MARITAL" in [r["rule_id"] for r in rules[:3]]


def test_top_k_prunes_to_most_relevant():
    """With k < rulebook size, retrieval prunes — and keeps the relevant rule."""
    rules = retrieve_rules(
        "only male candidates, smart girls, female preferred",
        "DL", k=3, backend="local",
    )
    assert len(rules) == 3
    assert "IND-GENDER-CODED" in _ids(rules)


# ── 3. Jurisdiction normalization (India) ────────────────────────────────────


def test_normalize_jurisdiction_variants():
    assert normalize_role_jurisdiction("Bengaluru, KA") == "KA"
    assert normalize_role_jurisdiction("KA") == "KA"
    assert normalize_role_jurisdiction("Karnataka") == "KA"
    assert normalize_role_jurisdiction("Mumbai") == "MH"
    assert normalize_role_jurisdiction("Hyderabad, TG") == "TG"
    assert normalize_role_jurisdiction("Remote-India") == CENTRAL
    assert normalize_role_jurisdiction("") == CENTRAL
