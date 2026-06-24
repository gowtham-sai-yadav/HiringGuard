"""Member B — seed the compliance rules KG into Supabase (pgvector).

Reads hireguard/rag/ruleset.json, normalizes each rule's prose jurisdiction to
a code (the `rules.jurisdiction` column the `match_rules` RPC filters on),
embeds the rule summary + each real detection hint with OpenAI
text-embedding-3-small (1536-dim), and upserts into the `rules` /
`rule_detection_hints` tables created by hireguard/rag/migrations.sql.

Idempotent: clears both tables first, so `make seed` can re-run safely.

    make migrate   # create tables + match_rules RPC (once)
    make seed      # python -m hireguard.rag.seed

Requires SUPABASE_URL, SUPABASE_KEY, and OPENAI_API_KEY in .env.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from hireguard.db import get_supabase
from hireguard.llm import get_embeddings
from hireguard.tools.retrieve_rules import (
    _SENTINELS,
    _SEVERITY_BASELINE,
    jurisdiction_code_for_seed,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [seed] %(message)s")
log = logging.getLogger("seed")

RULESET_PATH = Path(__file__).resolve().parent / "ruleset.json"


def _summary(rule: dict) -> str:
    return f"{rule['title']}. {rule.get('recommendation_template', '')}".strip()


def _vec(embedding: list[float]) -> str:
    """pgvector accepts its text input form: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def main() -> None:
    rules = json.loads(RULESET_PATH.read_text(encoding="utf-8"))["rules"]
    log.info("Loaded %d rules from %s", len(rules), RULESET_PATH.name)

    embeddings = get_embeddings()
    sb = get_supabase()

    # 1. Embed rule summaries (one batch call).
    summaries = [_summary(r) for r in rules]
    summary_vecs = embeddings.embed_documents(summaries)

    # 2. Embed every real (non-sentinel) detection hint (one batch call).
    hint_rows: list[tuple[str, str]] = []  # (rule_id, hint)
    for r in rules:
        for h in r.get("detection_hints", []):
            if h not in _SENTINELS:
                hint_rows.append((r["rule_id"], h))
    hint_vecs = embeddings.embed_documents([h for _, h in hint_rows]) if hint_rows else []
    log.info("Embedded %d summaries + %d hints", len(summary_vecs), len(hint_vecs))

    # 3. Idempotent reset (hints first — FK to rules).
    sb.table("rule_detection_hints").delete().neq("id", -1).execute()
    sb.table("rules").delete().neq("rule_id", "__none__").execute()

    # 4. Upsert rules.
    rule_payload = [
        {
            "rule_id": r["rule_id"],
            "title": r["title"],
            "jurisdiction": jurisdiction_code_for_seed(r["jurisdiction"]),
            "citation": r["citation"],
            "summary": _summary(r),
            "severity_baseline": _SEVERITY_BASELINE.get(
                str(r.get("category_default", "")).lower(), "medium"
            ),
            "rule_embedding": _vec(vec),
        }
        for r, vec in zip(rules, summary_vecs)
    ]
    sb.table("rules").upsert(rule_payload).execute()
    log.info("Upserted %d rules", len(rule_payload))

    # 5. Insert detection hints.
    if hint_rows:
        hint_payload = [
            {"rule_id": rid, "hint": hint, "hint_embedding": _vec(vec)}
            for (rid, hint), vec in zip(hint_rows, hint_vecs)
        ]
        sb.table("rule_detection_hints").insert(hint_payload).execute()
        log.info("Inserted %d detection hints", len(hint_payload))

    log.info("✓ Seed complete. jurisdictions: %s",
             sorted({p["jurisdiction"] for p in rule_payload}))


if __name__ == "__main__":
    main()
