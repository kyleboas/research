"""Embedding generation and persistence utilities."""

from __future__ import annotations

import logging
import random
import time
from typing import Iterable, Sequence

from ..config import Settings

LOGGER = logging.getLogger("research.processing.embeddings")


class EmbeddingError(RuntimeError):
    """Raised when embedding generation fails after retries."""


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(value):.12g}" for value in values) + "]"


def _chunked(items: Sequence[tuple[int, str]], batch_size: int) -> Iterable[Sequence[tuple[int, str]]]:
    for index in range(0, len(items), batch_size):
        yield items[index : index + batch_size]


def _embed_batch(
    *,
    client: object,
    model: str,
    texts: Sequence[str],
    max_retries: int,
    initial_backoff_s: float,
) -> list[list[float]]:
    for attempt in range(max_retries + 1):
        try:
            response = client.embeddings.create(model=model, input=list(texts))
            return [list(row.embedding) for row in response.data]
        except Exception as error:  # noqa: BLE001
            if attempt >= max_retries:
                raise EmbeddingError(
                    f"Failed to embed batch after {max_retries + 1} attempts"
                ) from error

            delay = initial_backoff_s * (2**attempt)
            jitter = random.uniform(0, delay * 0.2)
            sleep_seconds = delay + jitter
            LOGGER.warning(
                "Embedding API error on attempt %s/%s; retrying in %.2fs: %s",
                attempt + 1,
                max_retries + 1,
                sleep_seconds,
                error,
            )
            time.sleep(sleep_seconds)

    raise EmbeddingError("Unreachable retry state")


def embed_chunks(
    *,
    chunks: Sequence[tuple[int, str]],
    settings: Settings,
    model: str | None = None,
    batch_size: int = 64,
    max_retries: int = 4,
    initial_backoff_s: float = 1.0,
) -> list[tuple[int, list[float], str]]:
    """Generate embeddings for (chunk_id, content) records in batches."""

    if not chunks:
        return []

    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    selected_model = model or settings.openai_embedding_model

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)

    embedded_rows: list[tuple[int, list[float], str]] = []
    for chunk_batch in _chunked(chunks, batch_size):
        chunk_ids = [chunk_id for chunk_id, _ in chunk_batch]
        texts = [content for _, content in chunk_batch]
        vectors = _embed_batch(
            client=client,
            model=selected_model,
            texts=texts,
            max_retries=max_retries,
            initial_backoff_s=initial_backoff_s,
        )
        embedded_rows.extend(
            (chunk_id, vector, selected_model)
            for chunk_id, vector in zip(chunk_ids, vectors, strict=True)
        )

    return embedded_rows


def upsert_embeddings(connection: object, rows: Sequence[tuple[int, Sequence[float], str]]) -> int:
    """Upsert vectors by chunk_id + model into the embeddings table."""

    if not rows:
        return 0

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO embeddings (chunk_id, model, embedding)
            VALUES (%s, %s, %s::vector)
            ON CONFLICT (chunk_id, model)
            DO UPDATE SET
                embedding = EXCLUDED.embedding,
                updated_at = NOW()
            """,
            [
                (chunk_id, model, _vector_literal(vector))
                for chunk_id, vector, model in rows
            ],
        )

    return len(rows)
