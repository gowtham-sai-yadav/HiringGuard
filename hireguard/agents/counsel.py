"""CounselAgent — Member D's node.

Takes the scored findings produced upstream and writes the final `AuditMemo`:
an executive summary + one recommended fix per finding. This is the deliverable
a human reviews at the HITL gate.

Design decisions (defend these in viva):
  1. The severity COUNTS and the `needs_re_review` flag are computed in *code*,
     never trusted to the LLM — numbers the system reports must be exact.
  2. The LLM writes only the prose it is good at: the executive summary and the
     fix wording. We then overwrite the structured/numeric fields with the
     code-computed truth and re-attach the real `scored_findings` list.
  3. If no ANTHROPIC_API_KEY is set (CI, keyless dev) OR the LLM call fails, we
     fall back to a deterministic memo so the graph still runs end-to-end. The
     audit never hard-blocks on the memo writer.

`needs_re_review` drives Member A's conditional edge back to Policy: it is True
iff some critical finding has thin evidence (evidence_quality < 0.6).

DO NOT change the function signature or the return shape — A's graph depends on it.
"""
from __future__ import annotations

import logging
from pathlib import Path

from hireguard.llm import get_claude
from hireguard.settings import settings
from hireguard.state import (
    AuditMemo,
    PipelineState,
    RecommendedFix,
    ScoredFinding,
)

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "counsel.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text()

# Thin-evidence threshold: a critical finding below this evidence_quality forces a
# re-check loop back to Policy (bounded by MAX_REVISIONS in graph.py).
_THIN_EVIDENCE = 0.6

# Map severity → default fix priority when we have to build fixes deterministically.
_PRIORITY_BY_SEVERITY = {
    "critical": "must_fix",
    "high": "must_fix",
    "medium": "should_fix",
    "low": "nice_to_fix",
}


def _counts(scored: list[ScoredFinding]) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for s in scored:
        counts[s.severity] += 1
    return counts


def _needs_re_review(scored: list[ScoredFinding]) -> bool:
    return any(
        s.severity == "critical" and s.finding.evidence_quality < _THIN_EVIDENCE
        for s in scored
    )


def _findings_digest(scored: list[ScoredFinding]) -> str:
    """Render the scored findings into a compact, LLM-readable block."""
    if not scored:
        return "(no findings — the packet appears compliant)"
    lines = []
    for i, s in enumerate(scored, 1):
        f = s.finding
        lines.append(
            f"{i}. finding_id={f.finding_id} | rule_id={f.rule_id} | "
            f"severity={s.severity} | exposure={s.exposure_score} | "
            f"evidence_quality={f.evidence_quality:.2f}\n"
            f"   citation: {f.citation}\n"
            f"   evidence: \"{f.evidence_quote}\"\n"
            f"   rationale: {f.rationale}"
        )
    return "\n".join(lines)


def _deterministic_fixes(scored: list[ScoredFinding]) -> list[RecommendedFix]:
    return [
        RecommendedFix(
            finding_id=s.finding.finding_id,
            fix_text=(
                f"Address the {s.severity} compliance issue ({s.finding.rule_id}): "
                f"revise the language identified in the evidence and re-review."
            ),
            priority=_PRIORITY_BY_SEVERITY[s.severity],
        )
        for s in scored
    ]


def _deterministic_summary(counts: dict[str, int], n: int) -> str:
    if n == 0:
        return (
            "No compliance violations were detected in this hiring packet. "
            "The posting, compensation band, and interview scorecard appear to meet "
            "the applicable employment-law requirements. A human reviewer should "
            "confirm before finalizing."
        )
    return (
        f"This audit identified {n} compliance issue(s): {counts['critical']} critical, "
        f"{counts['high']} high, {counts['medium']} medium, and {counts['low']} low. "
        "Critical and high-severity items carry meaningful legal exposure and should be "
        "corrected before the role is posted. See the recommended fixes for the specific "
        "changes required, then route to a human reviewer for sign-off."
    )


def _build_memo(
    *,
    summary: str,
    fixes: list[RecommendedFix],
    scored: list[ScoredFinding],
    counts: dict[str, int],
    needs_re_review: bool,
) -> AuditMemo:
    """Assemble the final memo with code-computed numbers (LLM never sets these)."""
    return AuditMemo(
        executive_summary=summary,
        critical_count=counts["critical"],
        high_count=counts["high"],
        medium_count=counts["medium"],
        low_count=counts["low"],
        scored_findings=scored,
        recommended_fixes=fixes,
        needs_re_review=needs_re_review,
        re_review_reason=(
            "A critical finding has thin evidence (evidence_quality < 0.6); "
            "re-checking with Policy before human review."
            if needs_re_review
            else None
        ),
    )


def _align_fixes(
    llm_fixes: list[RecommendedFix], scored: list[ScoredFinding]
) -> list[RecommendedFix]:
    """Guarantee exactly one fix per finding, with the right finding_id + priority.

    We trust the LLM's `fix_text`, but enforce the structural contract in code so a
    sloppy model can never drop a finding or mislabel a priority.
    """
    by_id = {fx.finding_id: fx for fx in llm_fixes}
    aligned: list[RecommendedFix] = []
    for s in scored:
        fid = s.finding.finding_id
        priority = _PRIORITY_BY_SEVERITY[s.severity]
        llm_fix = by_id.get(fid)
        if llm_fix is not None and llm_fix.fix_text.strip():
            aligned.append(
                RecommendedFix(
                    finding_id=fid,
                    fix_text=llm_fix.fix_text.strip(),
                    priority=priority,
                )
            )
        else:
            aligned.append(_deterministic_fixes([s])[0])
    return aligned


async def counsel_node(state: PipelineState) -> dict:
    scored = state.scored_findings
    counts = _counts(scored)
    needs_re_review = _needs_re_review(scored)
    n = len(scored)
    errors: list[str] = []

    has_key = bool(settings()["ANTHROPIC_API_KEY"])

    summary: str
    fixes: list[RecommendedFix]

    if has_key:
        try:
            llm = get_claude().with_structured_output(AuditMemo)
            user_msg = (
                f"# SCORED FINDINGS ({n} total)\n\n"
                f"{_findings_digest(scored)}\n\n"
                "Write the executive_summary and one recommended_fix per finding "
                "(copy each finding_id exactly). The system will recompute all "
                "counts and the re-review flag — focus on clear prose."
            )
            draft: AuditMemo = await llm.ainvoke(
                [("system", SYSTEM_PROMPT), ("user", user_msg)]
            )
            summary = draft.executive_summary.strip() or _deterministic_summary(counts, n)
            fixes = _align_fixes(draft.recommended_fixes, scored)
        except Exception as exc:  # noqa: BLE001 — memo writer must never crash the audit
            log.warning("counsel_node: LLM call failed (%s); using deterministic memo", exc)
            errors.append(f"[counsel_node] LLM unavailable, used deterministic memo: {exc}")
            summary = _deterministic_summary(counts, n)
            fixes = _deterministic_fixes(scored)
    else:
        log.info("counsel_node: no ANTHROPIC_API_KEY; using deterministic memo")
        summary = _deterministic_summary(counts, n)
        fixes = _deterministic_fixes(scored)

    memo = _build_memo(
        summary=summary,
        fixes=fixes,
        scored=scored,
        counts=counts,
        needs_re_review=needs_re_review,
    )

    log.info(
        "counsel_node done: memo=%s findings=%s (c=%s h=%s m=%s l=%s) re_review=%s",
        memo.memo_id,
        n,
        counts["critical"],
        counts["high"],
        counts["medium"],
        counts["low"],
        needs_re_review,
    )

    return {
        "audit_memo": memo,
        "revision_count": state.revision_count + 1,
        "errors": errors,
    }
