BEGIN;

CREATE OR REPLACE FUNCTION hybrid_rrf_search(
    query_text TEXT,
    query_embedding VECTOR(1536),
    match_count INTEGER DEFAULT 20,
    candidate_multiplier INTEGER DEFAULT 5,
    rrf_k INTEGER DEFAULT 60,
    text_weight DOUBLE PRECISION DEFAULT 1.0,
    vector_weight DOUBLE PRECISION DEFAULT 1.0
)
RETURNS TABLE (
    chunk_id BIGINT,
    source_id BIGINT,
    combined_score DOUBLE PRECISION,
    text_rank INTEGER,
    vector_rank INTEGER,
    text_score DOUBLE PRECISION,
    vector_score DOUBLE PRECISION,
    combined_rank INTEGER
)
LANGUAGE SQL
STABLE
AS $$
WITH config AS (
    SELECT
        GREATEST(match_count, 1) AS k,
        GREATEST(candidate_multiplier, 1) AS mult,
        GREATEST(rrf_k, 1) AS rrf_constant,
        GREATEST(text_weight, 0) AS tw,
        GREATEST(vector_weight, 0) AS vw
),
text_candidates AS (
    SELECT
        c.id AS chunk_id,
        c.source_id,
        ts_rank_cd(c.search_tsv, plainto_tsquery('english', query_text))::DOUBLE PRECISION AS text_score,
        ROW_NUMBER() OVER (
            ORDER BY ts_rank_cd(c.search_tsv, plainto_tsquery('english', query_text)) DESC, c.id
        )::INTEGER AS text_rank
    FROM chunks c
    CROSS JOIN config cfg
    WHERE query_text IS NOT NULL
      AND BTRIM(query_text) <> ''
      AND c.search_tsv @@ plainto_tsquery('english', query_text)
    ORDER BY text_score DESC, c.id
    LIMIT (SELECT k * mult FROM config)
),
vector_candidates AS (
    SELECT
        c.id AS chunk_id,
        c.source_id,
        (1 - (e.embedding <=> query_embedding))::DOUBLE PRECISION AS vector_score,
        ROW_NUMBER() OVER (
            ORDER BY e.embedding <=> query_embedding ASC, c.id
        )::INTEGER AS vector_rank
    FROM embeddings e
    INNER JOIN chunks c ON c.id = e.chunk_id
    CROSS JOIN config cfg
    WHERE query_embedding IS NOT NULL
    ORDER BY e.embedding <=> query_embedding ASC, c.id
    LIMIT (SELECT k * mult FROM config)
),
fused AS (
    SELECT
        COALESCE(t.chunk_id, v.chunk_id) AS chunk_id,
        COALESCE(t.source_id, v.source_id) AS source_id,
        t.text_rank,
        v.vector_rank,
        t.text_score,
        v.vector_score,
        (
            (SELECT tw FROM config)
            * COALESCE(1.0 / ((SELECT rrf_constant FROM config) + t.text_rank), 0)
            +
            (SELECT vw FROM config)
            * COALESCE(1.0 / ((SELECT rrf_constant FROM config) + v.vector_rank), 0)
        )::DOUBLE PRECISION AS combined_score
    FROM text_candidates t
    FULL OUTER JOIN vector_candidates v
        ON t.chunk_id = v.chunk_id
),
ranked AS (
    SELECT
        f.*,
        ROW_NUMBER() OVER (ORDER BY f.combined_score DESC, f.chunk_id)::INTEGER AS combined_rank
    FROM fused f
)
SELECT
    r.chunk_id,
    r.source_id,
    r.combined_score,
    r.text_rank,
    r.vector_rank,
    r.text_score,
    r.vector_score,
    r.combined_rank
FROM ranked r
ORDER BY r.combined_score DESC, r.chunk_id
LIMIT (SELECT k FROM config);
$$;

COMMIT;
