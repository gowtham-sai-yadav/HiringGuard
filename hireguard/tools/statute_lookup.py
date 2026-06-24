"""Member B — Tavily statute-currency check (2nd real external API).

A "freshness" signal for each cited rule: is the statute still current per a live
web search? This is strictly best-effort enrichment — it must NEVER fail or slow
the audit:

  * Disabled unless HG_ENABLE_TAVILY=1 AND TAVILY_API_KEY is set.
  * Any error → returns is_current=True with a 'lookup_failed' marker.
  * @lru_cache keeps repeated demo runs cheap.

The result is attached to the run's `errors`/metadata channel (visible in the
LangSmith trace and the HITL panel) — it does not change which findings surface.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

try:  # tool decorator is optional at import time
    from langchain_core.tools import tool as _lc_tool
except Exception:  # pragma: no cover
    def _lc_tool(fn=None, **_kw):  # type: ignore
        def _wrap(f):
            return f
        return _wrap(fn) if callable(fn) else _wrap

log = logging.getLogger(__name__)

_client = None


def is_enabled() -> bool:
    return os.environ.get("HG_ENABLE_TAVILY") == "1" and bool(os.environ.get("TAVILY_API_KEY"))


def _tavily():
    global _client
    if _client is None:
        from tavily import TavilyClient

        _client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    return _client


@_lc_tool
@lru_cache(maxsize=128)
def verify_statute_currency(rule_id: str, citation: str, statute_short_name: str) -> dict:
    """Look up the cited statute on the live web and return a freshness signal:
    {is_current: bool, latest_version_url: str | None, snippet: str | None}.
    Best-effort: on any error returns is_current=True with a 'lookup_failed' marker."""
    if not is_enabled():
        return {"is_current": True, "latest_version_url": None, "snippet": "tavily_disabled"}
    query = f'"{statute_short_name}" current statute text 2026 amendments'
    try:
        res = _tavily().search(query=query, search_depth="basic", max_results=3)
        results = res.get("results") or []
        top = results[0] if results else {}
        return {
            "is_current": bool(top),
            "latest_version_url": top.get("url"),
            "snippet": (top.get("content") or "")[:200],
        }
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning("verify_statute_currency failed for %s: %s", rule_id, exc)
        return {"is_current": True, "latest_version_url": None, "snippet": f"lookup_failed: {exc}"}
