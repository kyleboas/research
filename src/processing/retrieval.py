"""Helpers for hybrid retrieval using SQL RRF search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(value):.12g}" for value in values) + "]"


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    source_id: int
    chunk_index: int
    content: str
    source_type: str
    source_key: str
    source_title: str | None
    source_metadata: dict[str, Any]
    chunk_metadata: dict[str, Any]
    combined_score: float
    text_rank: int | None
    vector_rank: int | None


def hybrid_search(
    connection: object,
    *,
    query_text: str,
    query_embedding: Sequence[float] | None,
    top_k: int = 20,
    candidate_multiplier: int = 5,
    rrf_k: int = 60,
    text_weight: float = 1.0,
    vector_weight: float = 1.0,
) -> list[RetrievedChunk]:
    """Return top-k chunks with source metadata using sql/003_hybrid_search.sql."""

    embedding_literal = _vector_literal(query_embedding) if query_embedding is not None else None

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                hs.chunk_id,
                hs.source_id,
                hs.combined_score,
                hs.text_rank,
                hs.vector_rank,
                c.chunk_index,
                c.content,
                c.metadata,
                s.source_type,
                s.source_key,
                s.title,
                s.metadata
            FROM hybrid_rrf_search(
                %s,
                %s::vector,
                %s,
                %s,
                %s,
                %s,
                %s
            ) AS hs
            INNER JOIN chunks AS c
                ON c.id = hs.chunk_id
            INNER JOIN sources AS s
                ON s.id = hs.source_id
            ORDER BY hs.combined_score DESC, hs.chunk_id
            """,
            (
                query_text,
                embedding_literal,
                top_k,
                candidate_multiplier,
                rrf_k,
                text_weight,
                vector_weight,
            ),
        )
        rows = cursor.fetchall()

    return [
        RetrievedChunk(
            chunk_id=row[0],
            source_id=row[1],
            combined_score=row[2],
            text_rank=row[3],
            vector_rank=row[4],
            chunk_index=row[5],
            content=row[6],
            chunk_metadata=row[7] or {},
            source_type=row[8],
            source_key=row[9],
            source_title=row[10],
            source_metadata=row[11] or {},
        )
        for row in rows
    ]
