CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS sources (
    id BIGSERIAL PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL UNIQUE,
    title TEXT,
    url TEXT,
    canonical_url TEXT,
    url_hash TEXT,
    content_hash TEXT,
    content TEXT NOT NULL,
    author TEXT,
    publish_date DATE,
    sitename TEXT,
    extraction_method TEXT DEFAULT 'rss',
    metadata JSONB DEFAULT '{}'::JSONB,
    search_tsv TSVECTOR GENERATED ALWAYS AS (
        TO_TSVECTOR('simple', COALESCE(title, '') || ' ' || COALESCE(LEFT(content, 2000), ''))
    ) STORED,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Migrate: add new columns to existing sources table
ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS author TEXT,
    ADD COLUMN IF NOT EXISTS publish_date DATE,
    ADD COLUMN IF NOT EXISTS sitename TEXT,
    ADD COLUMN IF NOT EXISTS extraction_method TEXT DEFAULT 'rss',
    ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::JSONB,
    ADD COLUMN IF NOT EXISTS canonical_url TEXT,
    ADD COLUMN IF NOT EXISTS url_hash TEXT,
    ADD COLUMN IF NOT EXISTS content_hash TEXT;

CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT REFERENCES sources(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    embedding VECTOR(1536),
    search_tsv TSVECTOR GENERATED ALWAYS AS (TO_TSVECTOR('simple', content)) STORED,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS reports (
    id BIGSERIAL PRIMARY KEY,
    title TEXT,
    content TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS report_policy_runs (
    id BIGSERIAL PRIMARY KEY,
    fixture_path TEXT NOT NULL,
    topic_count INT NOT NULL DEFAULT 0,
    topics JSONB NOT NULL DEFAULT '[]'::jsonb,
    baseline_score DOUBLE PRECISION NOT NULL,
    best_score DOUBLE PRECISION NOT NULL,
    delta DOUBLE PRECISION NOT NULL,
    estimated_cost_per_report DOUBLE PRECISION NOT NULL DEFAULT 0,
    quality_per_dollar DOUBLE PRECISION NOT NULL DEFAULT 0,
    min_improvement DOUBLE PRECISION NOT NULL DEFAULT 0,
    max_report_llm_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    applied BOOLEAN NOT NULL DEFAULT FALSE,
    apply_decision TEXT NOT NULL DEFAULT '',
    budget_status TEXT NOT NULL DEFAULT '',
    policy_changed BOOLEAN NOT NULL DEFAULT FALSE,
    baseline_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    best_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    topic_scores JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pipeline_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trend_candidates (
    id          BIGSERIAL PRIMARY KEY,
    trend_fingerprint TEXT,
    trend       TEXT NOT NULL,
    reasoning   TEXT,
    score       INT NOT NULL CHECK (score BETWEEN 0 AND 100),
    status      TEXT NOT NULL DEFAULT 'pending',
    detected_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE trend_candidates
    ADD COLUMN IF NOT EXISTS trend_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS feedback_adjustment INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS final_score INT;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'trend_candidates'::regclass
          AND conname = 'trend_candidates_status_check'
    ) THEN
        ALTER TABLE trend_candidates DROP CONSTRAINT trend_candidates_status_check;
    END IF;
EXCEPTION
    WHEN undefined_table THEN
        NULL;
END $$;

DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT c.conname
        FROM pg_constraint c
        WHERE c.conrelid = 'trend_candidates'::regclass
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) LIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE trend_candidates DROP CONSTRAINT %I', constraint_name);
    END LOOP;
EXCEPTION
    WHEN undefined_table THEN
        NULL;
END $$;

DO $$
BEGIN
    UPDATE trend_candidates
    SET status = 'needs_more_evidence'
    WHERE status = 'skipped';
EXCEPTION
    WHEN undefined_table THEN
        NULL;
END $$;

ALTER TABLE trend_candidates
    ADD CONSTRAINT trend_candidates_status_check
    CHECK (status IN ('pending', 'reported', 'needs_more_evidence'));

CREATE TABLE IF NOT EXISTS trend_candidate_sources (
    trend_candidate_id BIGINT NOT NULL REFERENCES trend_candidates(id) ON DELETE CASCADE,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (trend_candidate_id, source_id)
);

CREATE TABLE IF NOT EXISTS trend_feedback (
    id BIGSERIAL PRIMARY KEY,
    trend_candidate_id BIGINT REFERENCES trend_candidates(id) ON DELETE SET NULL,
    trend_text TEXT NOT NULL,
    feedback_value INT NOT NULL CHECK (feedback_value IN (-5, -1, 1, 5)),
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for hybrid search
CREATE INDEX IF NOT EXISTS idx_trend_candidates_status_score ON trend_candidates (status, score DESC);
CREATE INDEX IF NOT EXISTS idx_trend_candidates_final_score ON trend_candidates (COALESCE(final_score, score) DESC, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_trend_candidates_fingerprint ON trend_candidates (trend_fingerprint);
CREATE INDEX IF NOT EXISTS idx_trend_feedback_created_at ON trend_feedback (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_policy_runs_created_at ON report_policy_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS idx_sources_tsv ON sources USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS idx_sources_url_hash ON sources (url_hash);
CREATE INDEX IF NOT EXISTS idx_sources_content_hash ON sources (content_hash);

-- Tactical patterns extracted from chunks (actor → action → context)
CREATE TABLE IF NOT EXISTS tactical_patterns (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT REFERENCES sources(id) ON DELETE CASCADE,
    chunk_id BIGINT REFERENCES chunks(id) ON DELETE CASCADE,
    pattern_type TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    context TEXT,
    teams TEXT[],
    players TEXT[],
    zones TEXT[],
    phase TEXT,
    embedding VECTOR(1536),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tactical_patterns_created_at ON tactical_patterns (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tactical_patterns_type ON tactical_patterns (pattern_type);
CREATE INDEX IF NOT EXISTS idx_tactical_patterns_embedding ON tactical_patterns USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- Historical novelty baselines: embeddings of previously seen tactical concepts
CREATE TABLE IF NOT EXISTS novelty_baselines (
    id BIGSERIAL PRIMARY KEY,
    concept TEXT NOT NULL,
    embedding VECTOR(1536) NOT NULL,
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ DEFAULT NOW(),
    occurrence_count INT DEFAULT 1,
    source_count INT DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_novelty_baselines_embedding ON novelty_baselines USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- Add novelty_score column to trend_candidates
ALTER TABLE trend_candidates
    ADD COLUMN IF NOT EXISTS novelty_score DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS source_diversity INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pattern_ids BIGINT[];

-- BERTrend topic tracker state (JSON snapshot of TopicTracker)
CREATE TABLE IF NOT EXISTS topic_snapshots (
    id BIGSERIAL PRIMARY KEY,
    snapshot JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_topic_snapshots_created_at ON topic_snapshots (created_at DESC);

-- Hybrid Reciprocal Rank Fusion search
CREATE OR REPLACE FUNCTION hybrid_search(
    query_text TEXT,
    query_embedding VECTOR(1536),
    match_count INTEGER DEFAULT 20,
    rrf_k INTEGER DEFAULT 60
)
RETURNS TABLE (chunk_id BIGINT, source_id BIGINT, content TEXT, score DOUBLE PRECISION)
LANGUAGE SQL STABLE AS $$
WITH text_hits AS (
    SELECT c.id AS chunk_id, c.source_id, c.content,
           ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.search_tsv, plainto_tsquery('simple', query_text)) DESC, c.id) AS rank
    FROM chunks c
    WHERE c.search_tsv @@ plainto_tsquery('simple', query_text)
    LIMIT match_count * 3
),
vector_hits AS (
    SELECT c.id AS chunk_id, c.source_id, c.content,
           ROW_NUMBER() OVER (ORDER BY c.embedding <=> query_embedding, c.id) AS rank
    FROM chunks c
    WHERE c.embedding IS NOT NULL
    ORDER BY c.embedding <=> query_embedding
    LIMIT match_count * 3
),
fused AS (
    SELECT COALESCE(t.chunk_id, v.chunk_id) AS chunk_id,
           COALESCE(t.source_id, v.source_id) AS source_id,
           COALESCE(t.content, v.content) AS content,
           (COALESCE(1.0 / (rrf_k + t.rank), 0) + COALESCE(1.0 / (rrf_k + v.rank), 0)) AS score
    FROM text_hits t FULL OUTER JOIN vector_hits v ON t.chunk_id = v.chunk_id
)
SELECT chunk_id, source_id, content, score
FROM fused ORDER BY score DESC LIMIT match_count;
$$;
