"""Env-var loader. Single place that reads the environment."""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


# ──────────────────────────────────────────────────────────────────────────────
# LangSmith env normalization
# ──────────────────────────────────────────────────────────────────────────────
# LangSmith ships two parallel env-var conventions:
#   - LANGSMITH_* (current, preferred by the langsmith Python SDK)
#   - LANGCHAIN_* (legacy, still read by langchain-core / langgraph internals)
#
# We accept either set in .env and mirror them into BOTH namespaces so the entire
# stack (LangGraph traces, LangChain LLM clients, the langsmith SDK) finds the
# values it expects. If no API key is found, we hard-disable tracing so missing-
# key 401 noise never reaches the console.
def _mirror(primary: str, alias: str) -> None:
    """If `primary` is set and non-empty, copy it into `alias` (and vice-versa)."""
    pv, av = os.environ.get(primary, ""), os.environ.get(alias, "")
    if pv and not av:
        os.environ[alias] = pv
    elif av and not pv:
        os.environ[primary] = av


_mirror("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")
_mirror("LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT")
_mirror("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT")
_mirror("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")

# Auto-disable tracing if no key is present (prevents 401 spam in dev).
if not os.environ.get("LANGCHAIN_API_KEY"):
    for k in ("LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING"):
        os.environ[k] = "false"
    for k in ("LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT"):
        os.environ.pop(k, None)

# Allow our Pydantic models to round-trip through the LangGraph checkpointer
# without triggering the "unregistered type" deserialization warning.
os.environ.setdefault(
    "LANGGRAPH_ALLOWED_MSGPACK_MODULES",
    "hireguard.state",
)


def _required(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            f"Missing required env var: {key}. Copy .env.example to .env and fill it in."
        )
    return val


def _optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@lru_cache(maxsize=1)
def settings() -> dict:
    """Reads env vars lazily. Missing required keys raise only when *used*
    (see `require()` below) — so importing this module never fails."""
    return {
        # LLMs
        "ANTHROPIC_API_KEY": _optional("ANTHROPIC_API_KEY"),
        "GROQ_API_KEY": _optional("GROQ_API_KEY"),
        "OPENAI_API_KEY": _optional("OPENAI_API_KEY"),
        # Supabase
        "SUPABASE_URL": _optional("SUPABASE_URL"),
        "SUPABASE_KEY": _optional("SUPABASE_KEY"),
        "SUPABASE_DB_URL": _optional("SUPABASE_DB_URL"),
        # LangSmith (both naming conventions read; default project name)
        "LANGCHAIN_TRACING_V2": _optional("LANGCHAIN_TRACING_V2", "true"),
        "LANGCHAIN_PROJECT": _optional("LANGCHAIN_PROJECT", "HireGuard"),
        "LANGCHAIN_ENDPOINT": _optional("LANGCHAIN_ENDPOINT"),
        # Tavily (optional)
        "TAVILY_API_KEY": _optional("TAVILY_API_KEY"),
    }


def require(key: str) -> str:
    """Fetch a required setting; raise a clear error at the call site if missing."""
    val = settings().get(key, "")
    if not val:
        raise RuntimeError(
            f"Missing required env var: {key}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


def use_postgres_checkpointer() -> bool:
    """If SUPABASE_DB_URL is set, use PostgresSaver. Else fall back to MemorySaver."""
    return bool(settings()["SUPABASE_DB_URL"])


def langsmith_enabled() -> bool:
    """True iff we have a key AND tracing wasn't force-disabled."""
    return (
        bool(os.environ.get("LANGCHAIN_API_KEY"))
        and os.environ.get("LANGCHAIN_TRACING_V2", "").lower() == "true"
    )
