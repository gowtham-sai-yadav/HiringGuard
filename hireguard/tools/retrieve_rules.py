"""Member B — rule retrieval (the RAG / knowledge-grounding layer).

Scope: INDIAN hiring-compliance law (see hireguard/rag/ruleset.json). Most of
India's employment-equality law is Union/Central (applies everywhere), so the
always-include jurisdiction is `India-Central`; state codes ('KA', 'MH', 'DL',
'TN', …) are reserved for future state-specific rules.

PolicyAgent does NOT see the whole rulebook. It sees only the rules that are
(a) applicable to the role's jurisdiction and (b) ranked by relevance to the
extracted facts. That is the rubric's "RAG used where it improves grounding".

HYBRID retrieval — two signals:

  1. Jurisdiction filter (SQL `WHERE` / code-set test)
       Always include India-Central rules + rules that bind the role's state.
  2. Semantic match (pgvector cosine in prod, lexical overlap locally)
       Rank applicable rules by relevance to a query built from the facts, so a
       larger rulebook prunes to the top-K most relevant.

Two backends behind one interface:

  * LocalRetriever     — loads ruleset.json, no DB / no API key, fully
                         deterministic. Used by tests, CI, and the offline demo.
  * SupabaseRetriever  — pgvector cosine via the `match_rules` RPC (which filters
                         `jurisdiction IN (match_jurisdiction, 'India-Central')`).

`get_retriever()` picks SupabaseRetriever when Supabase + an embeddings key are
configured, else LocalRetriever — so the slice always runs.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

# `@tool` is optional at import time so tests can run with only stdlib present.
try:  # pragma: no cover - exercised indirectly
    from langchain_core.tools import tool as _lc_tool
except Exception:  # pragma: no cover
    def _lc_tool(fn=None, **_kw):  # type: ignore
        def _wrap(f):
            return f
        return _wrap(fn) if callable(fn) else _wrap


RULESET_PATH = Path(__file__).resolve().parent.parent / "rag" / "ruleset.json"

# This ruleset has no absence-sentinel hints (unlike the old US pay-transparency
# rules). Kept as an empty set so seed.py's hint filter stays a no-op.
_SENTINELS: set[str] = set()

# category_default (ruleset vocab) → severity baseline (C's vocab).
_SEVERITY_BASELINE = {
    "critical": "critical",
    "risk": "high",
    "gap": "medium",
    "suggestion": "low",
}

# ──────────────────────────────────────────────────────────────────────────────
# Jurisdiction normalization (India)
# ──────────────────────────────────────────────────────────────────────────────

CENTRAL = "India-Central"  # Union/Central law — always included.

# Indian state codes + the major cities that map to them.
_STATE_NAME_TO_CODE = {
    "karnataka": "KA", "bengaluru": "KA", "bangalore": "KA",
    "maharashtra": "MH", "mumbai": "MH", "pune": "MH",
    "delhi": "DL", "new delhi": "DL",
    "tamil nadu": "TN", "chennai": "TN",
    "telangana": "TG", "hyderabad": "TG",
    "kerala": "KL", "kochi": "KL",
    "west bengal": "WB", "kolkata": "WB",
    "gujarat": "GJ", "ahmedabad": "GJ",
    "uttar pradesh": "UP", "noida": "UP",
    "haryana": "HR", "gurugram": "HR", "gurgaon": "HR",
    "rajasthan": "RJ", "jaipur": "RJ",
    "andhra pradesh": "AP",
}
_VALID_STATE_CODES = set(_STATE_NAME_TO_CODE.values())


def normalize_role_jurisdiction(raw: Optional[str]) -> str:
    """A role's location string → a single code we can filter on.

    Accepts codes ("KA"), prose ("Bengaluru, KA"), full names ("Karnataka"),
    cities ("Mumbai"), and "Remote-India". Anything unrecognised → India-Central
    (so a role still gets the Union/Central rules and never an empty result).
    """
    if not raw:
        return CENTRAL
    s = raw.strip()
    if s.upper() in {"REMOTE-INDIA", "REMOTE", "INDIA", "IN", "CENTRAL", "INDIA-CENTRAL"}:
        return CENTRAL
    # Exact 2-letter code.
    if len(s) == 2 and s.upper() in _VALID_STATE_CODES:
        return s.upper()
    # Trailing ", KA" style.
    m = re.search(r",\s*([A-Za-z]{2})\b\s*$", s)
    if m and m.group(1).upper() in _VALID_STATE_CODES:
        return m.group(1).upper()
    # Any 2-letter token that is a known state code.
    for tok in re.findall(r"\b([A-Za-z]{2})\b", s):
        if tok.upper() in _VALID_STATE_CODES:
            return tok.upper()
    # Full state / city name.
    low = s.lower()
    for name, code in _STATE_NAME_TO_CODE.items():
        if name in low:
            return code
    return CENTRAL


def rule_jurisdiction_codes(prose: str) -> set[str]:
    """A rule's `jurisdiction` prose → the set of codes it binds."""
    p = (prose or "").strip().lower()
    if p in {"india-central", "central", "india", ""} or "central" in p:
        return {CENTRAL}
    for name, code in _STATE_NAME_TO_CODE.items():
        if name in p:
            return {code}
    codes = {c for c in re.findall(r"\b([A-Z]{2})\b", prose) if c in _VALID_STATE_CODES}
    return codes or {CENTRAL}


def jurisdiction_code_for_seed(prose: str) -> str:
    """Single code stored in the `rules.jurisdiction` column by seed.py.

    Central rules → 'India-Central' (the RPC always includes it); single-state
    rules → their code.
    """
    codes = rule_jurisdiction_codes(prose)
    if codes == {CENTRAL}:
        return CENTRAL
    if len(codes) == 1:
        return next(iter(codes))
    return CENTRAL


def _applies(role_code: str, rule_codes: set[str]) -> bool:
    """Central law always applies; otherwise the role's state must be in the set."""
    return CENTRAL in rule_codes or role_code in rule_codes


# ──────────────────────────────────────────────────────────────────────────────
# Ruleset loading + payload shaping
# ──────────────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _load_rules() -> list[dict]:
    raw = json.loads(RULESET_PATH.read_text(encoding="utf-8"))
    return raw.get("rules", [])


def _summary(rule: dict) -> str:
    return f"{rule['title']}. {rule.get('recommendation_template', '')}".strip()


def _real_hints(rule: dict) -> list[str]:
    return [h for h in rule.get("detection_hints", []) if h not in _SENTINELS]


def _payload(rule: dict, *, match_reason: str, similarity: Optional[float] = None) -> dict:
    """Bounded, uniform shape handed to PolicyAgent (not the raw rulebook)."""
    return {
        "rule_id": rule["rule_id"],
        "title": rule["title"],
        "jurisdiction": rule.get("jurisdiction", ""),
        "jurisdiction_codes": sorted(rule_jurisdiction_codes(rule.get("jurisdiction", ""))),
        "citation": rule["citation"],
        "summary": _summary(rule),
        "severity_baseline": _SEVERITY_BASELINE.get(
            str(rule.get("category_default", "")).lower(), "medium"
        ),
        "detection_hints": _real_hints(rule),
        "recommendation_template": rule.get("recommendation_template", ""),
        "applies_to": rule.get("applies_to", []),
        "match_reason": match_reason,
        "similarity": similarity,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Lexical scoring (LocalRetriever's stand-in for cosine)
# ──────────────────────────────────────────────────────────────────────────────

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _lexical_score(query: str, rule: dict) -> float:
    """Deterministic relevance: token overlap + multiword-phrase substring bonus."""
    q = query.lower()
    q_tokens = set(_tokens(query))
    if not q_tokens:
        return 0.0
    hint_text = " ".join(_real_hints(rule))
    rule_text = f"{rule['title']} {_summary(rule)} {hint_text}"
    r_tokens = _tokens(rule_text)
    overlap = sum(1 for t in r_tokens if t in q_tokens)
    # Strong bonus when a full detection-hint phrase appears in the query.
    phrase_bonus = sum(3 for h in _real_hints(rule) if h.strip() and h.lower() in q)
    return float(overlap) + float(phrase_bonus)


# ──────────────────────────────────────────────────────────────────────────────
# Retrievers
# ──────────────────────────────────────────────────────────────────────────────


class _BaseRetriever:
    """Shared jurisdiction filter + de-dup/cap."""

    def _applicable(self, role_code: str) -> list[dict]:
        return [
            r for r in _load_rules()
            if _applies(role_code, rule_jurisdiction_codes(r.get("jurisdiction", "")))
        ]

    def _cap(self, ranked: list[dict], k: int) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        for item in ranked:
            if item["rule_id"] in seen:
                continue
            seen.add(item["rule_id"])
            out.append(item)
        return out[:k]


class LocalRetriever(_BaseRetriever):
    """No DB, no API key, deterministic. The default for tests / offline demo."""

    backend = "local"

    def retrieve(self, query: str, jurisdiction: str, k: int = 10) -> list[dict]:
        role_code = normalize_role_jurisdiction(jurisdiction)
        applicable = self._applicable(role_code)
        ranked = sorted(applicable, key=lambda r: (-_lexical_score(query, r), r["rule_id"]))
        payloads = [
            _payload(r, match_reason="jurisdiction+lexical", similarity=_lexical_score(query, r))
            for r in ranked
        ]
        return self._cap(payloads, k)


class SupabaseRetriever(_BaseRetriever):
    """pgvector cosine via the `match_rules` RPC (jurisdiction-filtered)."""

    backend = "supabase"

    def retrieve(self, query: str, jurisdiction: str, k: int = 10) -> list[dict]:
        from hireguard.db import get_supabase
        from hireguard.llm import get_embeddings

        role_code = normalize_role_jurisdiction(jurisdiction)
        embedding = get_embeddings().embed_query(query)
        sb = get_supabase()
        res = sb.rpc(
            "match_rules",
            {
                "query_embedding": embedding,
                "match_jurisdiction": role_code,
                "match_count": k,
            },
        ).execute()

        # The RPC returns a bounded column set; enrich from the local ruleset so
        # the payload shape is identical to LocalRetriever's.
        by_id = {r["rule_id"]: r for r in _load_rules()}
        payloads: list[dict] = []
        for row in res.data or []:
            rule = by_id.get(row["rule_id"])
            if rule:
                payloads.append(
                    _payload(rule, match_reason="jurisdiction+pgvector",
                             similarity=row.get("similarity"))
                )
        return self._cap(payloads, k)


def _supabase_ready() -> bool:
    return bool(os.environ.get("SUPABASE_URL")) and bool(os.environ.get("SUPABASE_KEY")) \
        and bool(os.environ.get("OPENAI_API_KEY"))


@lru_cache(maxsize=2)
def get_retriever(backend: Optional[str] = None) -> _BaseRetriever:
    """Pick a backend. Explicit `backend` wins; else auto-detect by env."""
    if backend == "local":
        return LocalRetriever()
    if backend == "supabase":
        return SupabaseRetriever()
    if backend is None and _supabase_ready() and os.environ.get("HG_RETRIEVER", "auto") != "local":
        return SupabaseRetriever()
    return LocalRetriever()


def retrieve_rules(
    query: str,
    jurisdiction: str,
    k: int = 10,
    *,
    backend: Optional[str] = None,
) -> list[dict]:
    """Hybrid rule retrieval. The function PolicyAgent and tests call directly."""
    return get_retriever(backend).retrieve(query, jurisdiction, k)


@_lc_tool
def retrieve_relevant_rules(query: str, jurisdiction: str, k: int = 10) -> list[dict]:
    """Retrieve Indian hiring-compliance rules relevant to the given facts and
    jurisdiction. Returns a bounded list of rules (rule_id, title, citation,
    summary, jurisdiction), filtered by jurisdiction and ranked by relevance —
    not the entire rulebook."""
    return retrieve_rules(query, jurisdiction, k)
