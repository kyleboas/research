BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_embeddings_embedding_ivfflat_cosine
    ON embeddings USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_chunks_search_tsv
    ON chunks USING GIN (search_tsv);

CREATE INDEX IF NOT EXISTS idx_sources_search_tsv
    ON sources USING GIN (search_tsv);

CREATE INDEX IF NOT EXISTS idx_sources_source_key_trgm
    ON sources USING GIN (source_key gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_sources_normalized_source_key
    ON sources (normalized_source_key);

COMMIT;
