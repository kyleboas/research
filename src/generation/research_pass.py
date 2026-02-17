"""Research pass: curated retrieval query execution and context assembly."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Sequence

from openai import OpenAI

from ..config import Settings
from ..processing.retrieval import RetrievedChunk, hybrid_search

CURATED_QUERY_AREAS: tuple[str, ...] = (
    "latest developments and announcements",
    "technical methods and implementation details",
    "limitations, risks, and failure modes",
    "business, product, and ecosystem implications",
)


@dataclass(frozen=True)
class ContextChunk:
    source_id: int
    chunk_id: int
    chunk_index: int
    citation_id: str
    source_type: str
    source_key: str
    title: str | None
    combined_score: float
    text: str


@dataclass(frozen=True)
class ContextPacket:
    topic: str
    queries: list[str]
    chunks: list[ContextChunk]

    def to_json(self) -> str:
        return json.dumps(
            {
                "topic": self.topic,
                "queries": self.queries,
                "chunks": [asdict(chunk) for chunk in self.chunks],
            },
            indent=2,
            sort_keys=True,
        )


def _build_curated_queries(topic: str) -> list[str]:
    base = [f"{topic} {area}" for area in CURATED_QUERY_AREAS]
    base.append(f"{topic} notable quantitative claims and benchmarks")
    base.append(f"{topic} open questions and unresolved debates")
    return base


def _embed_query(query_text: str, settings: Settings) -> Sequence[float]:
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.embeddings.create(model=settings.openai_embedding_model, input=[query_text])
    return list(response.data[0].embedding)


def _to_context_chunk(chunk: RetrievedChunk) -> ContextChunk:
    return ContextChunk(
        source_id=chunk.source_id,
        chunk_id=chunk.chunk_id,
        chunk_index=chunk.chunk_index,
        citation_id=f"S{chunk.source_id}:C{chunk.chunk_id}",
        source_type=chunk.source_type,
        source_key=chunk.source_key,
        title=chunk.source_title,
        combined_score=chunk.combined_score,
        text=chunk.content,
    )


def run_research_pass(
    connection: object,
    *,
    topic: str,
    settings: Settings,
    top_k_per_query: int = 8,
    max_context_chunks: int = 30,
) -> ContextPacket:
    """Execute curated retrieval queries and assemble a deduplicated context packet."""

    queries = _build_curated_queries(topic)
    by_chunk_id: dict[int, ContextChunk] = {}

    for query in queries:
        query_embedding = _embed_query(query, settings)
        matches = hybrid_search(
            connection,
            query_text=query,
            query_embedding=query_embedding,
            top_k=top_k_per_query,
        )
        for match in matches:
            current = _to_context_chunk(match)
            previous = by_chunk_id.get(current.chunk_id)
            if previous is None or current.combined_score > previous.combined_score:
                by_chunk_id[current.chunk_id] = current

    selected = sorted(by_chunk_id.values(), key=lambda chunk: chunk.combined_score, reverse=True)
    return ContextPacket(topic=topic, queries=queries, chunks=selected[:max_context_chunks])
