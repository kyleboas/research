"""Claim extraction utilities for report verification."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

_CITATION_PATTERN = re.compile(r"\[S\d+:C(\d+)\]")
_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ExtractedClaim:
    """An atomic claim extracted from report markdown."""

    claim_id: str
    text: str
    cited_chunk_ids: tuple[int, ...]


def _clean_sentence(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^[-*+]\s+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _split_atomic_candidates(sentence: str) -> Iterable[str]:
    for part in sentence.split(";"):
        candidate = part.strip()
        if candidate:
            yield candidate


def extract_claims(markdown: str) -> list[ExtractedClaim]:
    """Extract atomic claims with deterministic IDs from markdown.

    Claims are currently sentence-like fragments that include at least one inline
    citation in the required format: ``[S<source_id>:C<chunk_id>]``.
    """

    claims: list[ExtractedClaim] = []
    in_code_block = False

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()

        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        for sentence in _SENTENCE_SPLIT_PATTERN.split(stripped):
            normalized_sentence = _clean_sentence(sentence)
            if not normalized_sentence:
                continue

            cited_ids = tuple(sorted({int(chunk_id) for chunk_id in _CITATION_PATTERN.findall(normalized_sentence)}))
            if not cited_ids:
                continue

            for atomic_part in _split_atomic_candidates(normalized_sentence):
                atomic_text = _clean_sentence(atomic_part)
                if not atomic_text:
                    continue

                claims.append(
                    ExtractedClaim(
                        claim_id=f"CLM-{len(claims) + 1:04d}",
                        text=atomic_text,
                        cited_chunk_ids=cited_ids,
                    )
                )

    return claims
