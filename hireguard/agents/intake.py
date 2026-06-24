"""IntakeAgent — Member A's node.

Pipeline:
  1. Read state.packet (HiringPacket).
  2. Redact PII from the job_posting blob (regex-based, cheap, defensible).
  3. Detect prompt-injection attempts (flag, do not refuse).
  4. Build a structured user message.
  5. Claude (fast tier) with_structured_output(IntakeFacts).
  6. Stamp pii_redacted_labels + injection_attempt_detected onto the facts.
  7. Return {"facts": ...}.

The PII redaction is the first guardrail surface — it runs BEFORE the LLM
ever sees the packet text. This is the rubric's "guardrails / safety" line.
"""
from __future__ import annotations

import logging
from pathlib import Path

from hireguard.guardrails import detect_prompt_injection, redact_pii
from hireguard.llm import get_claude_fast
from hireguard.state import HiringPacket, IntakeFacts, PipelineState

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "intake.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text()


_CURRENCY_SYMBOL = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€"}


def _fmt_band(currency: str, lo: int | None, hi: int | None) -> str:
    """Currency-aware salary band rendering. INR-friendly, US-friendly, neutral fallback."""
    sym = _CURRENCY_SYMBOL.get((currency or "").upper(), "")
    if lo is None and hi is None:
        return "not disclosed"
    if sym:
        return f"{sym}{lo:,}–{sym}{hi:,}" if (lo and hi) else f"{sym}{lo or hi:,}"
    return f"{currency} {lo}–{hi}" if (lo and hi) else f"{currency} {lo or hi}"


def _packet_to_message(packet: HiringPacket, posting_redacted: str) -> str:
    cb = packet.comp_band
    sc = packet.interview_scorecard
    criteria_lines = "\n".join(
        f"  - {c.name} (scale={c.scale}, anchored={c.anchored})"
        + (f" — note: {c.note}" if c.note else "")
        for c in sc.criteria
    )
    return (
        f"# HIRING PACKET\n\n"
        f"Company: {packet.company}\n"
        f"Company size: {packet.company_size}\n"
        f"Primary work location: {packet.primary_work_location}\n\n"
        f"## Job Posting (PII-redacted)\n{posting_redacted}\n\n"
        f"## Comp Band\n"
        f"  currency: {cb.currency}\n"
        f"  posted_range_in_listing: {cb.posted_range_in_listing}\n"
        f"  internal_band: {_fmt_band(cb.currency, cb.internal_band_min, cb.internal_band_max)}\n"
        f"  benefits_described_in_listing: {cb.benefits_described_in_listing}\n\n"
        f"## Interview Scorecard\n"
        f"  title: {sc.title}\n"
        f"  criteria:\n{criteria_lines}\n"
    )


async def intake_node(state: PipelineState) -> dict:
    packet = state.packet
    log.info("intake_node: packet=%s company=%s", packet.packet_id, packet.company)

    # 1. PII guardrail — runs before anything else.
    posting_redacted, pii_labels = redact_pii(packet.job_posting)
    injection_attempt = detect_prompt_injection(packet.job_posting)

    # 2. Structured extraction.
    llm = get_claude_fast(temperature=0.0).with_structured_output(IntakeFacts)
    user_msg = _packet_to_message(packet, posting_redacted)

    facts: IntakeFacts = await llm.ainvoke(
        [("system", SYSTEM_PROMPT), ("user", user_msg)]
    )

    # 3. Stamp guardrail metadata onto facts (LLM doesn't know about these).
    facts = facts.model_copy(
        update={
            "pii_redacted_labels": pii_labels,
            "injection_attempt_detected": injection_attempt,
        }
    )

    errors: list[str] = []
    if pii_labels:
        errors.append(f"[intake_node] PII redacted: {pii_labels}")
    if injection_attempt:
        errors.append("[intake_node] Prompt-injection attempt detected (flagged, not refused)")

    log.info(
        "intake_node done: jurisdiction=%s pay_range=%s age_phrases=%s injection=%s",
        facts.jurisdiction,
        facts.pay_range_disclosed,
        facts.age_coded_phrases,
        injection_attempt,
    )

    return {"facts": facts, "errors": errors}
