CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS sources (
    id BIGSERIAL PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL UNIQUE,
    title TEXT,
    url TEXT,
    content TEXT NOT NULL,
    search_tsv TSVECTOR GENERATED ALWAYS AS (
        TO_TSVECTOR('english', COALESCE(title, '') || ' ' || COALESCE(LEFT(content, 2000), ''))
    ) STORED,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT REFERENCES sources(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    embedding VECTOR(1536),
    search_tsv TSVECTOR GENERATED ALWAYS AS (TO_TSVECTOR('english', content)) STORED,
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

CREATE TABLE IF NOT EXISTS pipeline_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trend_candidates (
    id          BIGSERIAL PRIMARY KEY,
    trend       TEXT NOT NULL,
    reasoning   TEXT,
    score       INT NOT NULL CHECK (score BETWEEN 0 AND 100),
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'reported', 'skipped')),
    detected_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE trend_candidates
    ADD COLUMN IF NOT EXISTS feedback_adjustment INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS final_score INT;

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
CREATE INDEX IF NOT EXISTS idx_trend_feedback_created_at ON trend_feedback (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS idx_sources_tsv ON sources USING GIN (search_tsv);

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
           ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.search_tsv, plainto_tsquery('english', query_text)) DESC, c.id) AS rank
    FROM chunks c
    WHERE c.search_tsv @@ plainto_tsquery('english', query_text)
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
