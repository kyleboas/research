-- Migration: switch full-text search from English-only to language-agnostic 'simple'
-- config so content in any language is indexed and searchable.
--
-- Generated columns cannot be altered in-place; we must drop and re-add them.
-- Run this once against an existing database. New deployments can use schema.sql directly.

BEGIN;

-- sources.search_tsv
ALTER TABLE sources DROP COLUMN IF EXISTS search_tsv;
ALTER TABLE sources
    ADD COLUMN search_tsv TSVECTOR GENERATED ALWAYS AS (
        TO_TSVECTOR('simple', COALESCE(title, '') || ' ' || COALESCE(LEFT(content, 2000), ''))
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_sources_tsv ON sources USING GIN (search_tsv);

-- chunks.search_tsv
ALTER TABLE chunks DROP COLUMN IF EXISTS search_tsv;
ALTER TABLE chunks
    ADD COLUMN search_tsv TSVECTOR GENERATED ALWAYS AS (
        TO_TSVECTOR('simple', content)
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (search_tsv);

-- Update the hybrid search function to match
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

COMMIT;
