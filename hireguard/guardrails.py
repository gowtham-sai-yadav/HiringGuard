"""Cheap input guardrails. Member C will extend with output validators.

Run `redact_pii` at the top of the intake node before the LLM sees the packet.
Run `detect_prompt_injection` to flag (not refuse) — the packet is itself a
hiring document, so an injection attempt is something Policy should *also*
hear about.
"""
from __future__ import annotations

import re

# `@traceable` makes these helpers visible as spans in the LangSmith trace tree.
# Import this way (not directly from `langsmith`) so that when tracing is disabled
# the decorator becomes a cheap no-op rather than a missing import.
try:
    from langsmith import traceable
except ImportError:  # pragma: no cover — defensive

    def traceable(*_args, **_kwargs):  # type: ignore[no-redef]
        def _wrap(fn):
            return fn
        return _wrap

SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE_RE = re.compile(
    r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)
DOB_RE = re.compile(
    r"\b(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}\b"
)

_PATTERNS = [("SSN", SSN_RE), ("EMAIL", EMAIL_RE), ("PHONE", PHONE_RE), ("DOB", DOB_RE)]

INJECTION_PHRASES = [
    "ignore the ruleset",
    "ignore previous instructions",
    "ignore all rules",
    "approve regardless",
    "bypass the audit",
    "disregard the rubric",
]


@traceable(name="redact_pii", run_type="tool", tags=["guardrail", "pii"])
def redact_pii(text: str) -> tuple[str, list[str]]:
    """Replace PII patterns with [REDACTED_LABEL]. Returns (cleaned_text, labels_found)."""
    found: list[str] = []
    for label, pat in _PATTERNS:
        if pat.search(text):
            found.append(label)
            text = pat.sub(f"[REDACTED_{label}]", text)
    return text, found


@traceable(name="detect_prompt_injection", run_type="tool", tags=["guardrail", "injection"])
def detect_prompt_injection(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in INJECTION_PHRASES)
