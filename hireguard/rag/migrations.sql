-- HireGuard v2 — base schema
-- Run once via Supabase SQL editor, or `make migrate`.

-- ─── Audit-memo history (Member A — persist node writes here after HITL) ───
CREATE TABLE IF NOT EXISTS audit_memos (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      text NOT NULL,
    packet      jsonb NOT NULL,
    memo        jsonb NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS audit_memos_run_id_idx ON audit_memos (run_id);
CREATE INDEX IF NOT EXISTS audit_memos_approved_at_idx ON audit_memos (approved_at DESC);

-- ─── Compliance rules KG (Member B — seed.py populates this) ────────────────
-- pgvector extension provides the `vector` type.
CREATE EXTENSION IF NOT EXISTS vector;

-- TODO(Member B): project pivoted to INDIAN law on 2026-06-24 (ruleset.json is
-- now Indian). The always-include jurisdiction below is hardcoded as 'US-FED' in
-- match_rules() — change it to 'India-Central'. State codes are now Indian
-- ('KA', 'MH', 'DL', 'TN', ...). — Member C (Harsh)
CREATE TABLE IF NOT EXISTS rules (
    rule_id            text PRIMARY KEY,
    title              text NOT NULL,
    jurisdiction       text NOT NULL,        -- 'India-Central', 'KA', 'MH', 'DL', ...
    citation           text NOT NULL,
    summary            text NOT NULL,
    severity_baseline  text NOT NULL,
    rule_embedding     vector(1536)
);

CREATE TABLE IF NOT EXISTS rule_detection_hints (
    id              bigserial PRIMARY KEY,
    rule_id         text REFERENCES rules(rule_id) ON DELETE CASCADE,
    hint            text NOT NULL,
    hint_embedding  vector(1536)
);

CREATE INDEX IF NOT EXISTS rules_jurisdiction_idx ON rules (jurisdiction);
CREATE INDEX IF NOT EXISTS rules_embedding_idx ON rules
    USING ivfflat (rule_embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS hints_embedding_idx ON rule_detection_hints
    USING ivfflat (hint_embedding vector_cosine_ops) WITH (lists = 100);

-- ─── RPC for Member B's retrieve_relevant_rules tool ────────────────────────
CREATE OR REPLACE FUNCTION match_rules(
    query_embedding vector(1536),
    match_jurisdiction text,
    match_count int DEFAULT 10
)
RETURNS TABLE (
    rule_id text,
    title text,
    citation text,
    summary text,
    jurisdiction text,
    severity_baseline text,
    similarity float
)
LANGUAGE sql STABLE
AS $$
    SELECT
        r.rule_id, r.title, r.citation, r.summary,
        r.jurisdiction, r.severity_baseline,
        1 - (r.rule_embedding <=> query_embedding) AS similarity
    FROM rules r
    WHERE r.jurisdiction IN (match_jurisdiction, 'India-Central')  -- was 'US-FED' (pre-India pivot)
       OR match_jurisdiction = 'ANY'
    ORDER BY r.rule_embedding <=> query_embedding
    LIMIT match_count;
$$;
