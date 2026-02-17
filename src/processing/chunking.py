"""Utilities for splitting source text into retrieval-ready chunks."""

from __future__ import annotations

from dataclasses import dataclass
import re

_TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


@dataclass(frozen=True)
class Chunk:
    """A deterministic chunk of source text."""

    chunk_index: int
    content: str
    token_count: int


def approximate_token_count(text: str) -> int:
    """Estimate token count with a lightweight regex tokenization heuristic."""

    return len(_TOKEN_PATTERN.findall(text))


def chunk_text(text: str, *, window_size: int = 200, overlap: int = 40) -> list[Chunk]:
    """Split text into deterministic, overlapping chunks.

    Args:
        text: Raw source text to chunk.
        window_size: Number of words in each chunk window.
        overlap: Number of words shared between adjacent chunks.
    """

    if window_size <= 0:
        raise ValueError("window_size must be greater than zero")
    if overlap < 0:
        raise ValueError("overlap must be non-negative")
    if overlap >= window_size:
        raise ValueError("overlap must be less than window_size")

    words = text.split()
    if not words:
        return []

    step = window_size - overlap
    chunks: list[Chunk] = []

    for start in range(0, len(words), step):
        end = min(start + window_size, len(words))
        chunk_content = " ".join(words[start:end]).strip()
        if not chunk_content:
            continue

        chunks.append(
            Chunk(
                chunk_index=len(chunks),
                content=chunk_content,
                token_count=approximate_token_count(chunk_content),
            )
        )

        if end >= len(words):
            break

    return chunks
