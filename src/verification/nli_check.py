"""Claim-to-evidence verification using citation-constrained overlap scoring."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .claims import ExtractedClaim

_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9]+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "was",
    "were",
    "with",
}


@dataclass(frozen=True)
class ClaimCheckResult:
    claim_id: str
    status: str
    score: float
    evaluated_chunk_ids: tuple[int, ...]


def _tokenize(text: str) -> set[str]:
    tokens = {token.lower() for token in _TOKEN_PATTERN.findall(text)}
    return {token for token in tokens if len(token) > 2 and token not in _STOPWORDS}


def check_claims_against_citations(
    claims: list[ExtractedClaim],
    chunk_text_by_id: dict[int, str],
) -> list[ClaimCheckResult]:
    """Verify claims against *only* cited chunks.

    Returns one status per claim: ``supported``, ``uncertain``, or ``unsupported``.
    """

    results: list[ClaimCheckResult] = []

    for claim in claims:
        claim_tokens = _tokenize(claim.text)
        if not claim_tokens:
            results.append(
                ClaimCheckResult(
                    claim_id=claim.claim_id,
                    status="uncertain",
                    score=0.0,
                    evaluated_chunk_ids=tuple(),
                )
            )
            continue

        evaluated_chunk_ids = tuple(chunk_id for chunk_id in claim.cited_chunk_ids if chunk_id in chunk_text_by_id)
        if not evaluated_chunk_ids:
            results.append(
                ClaimCheckResult(
                    claim_id=claim.claim_id,
                    status="unsupported",
                    score=0.0,
                    evaluated_chunk_ids=tuple(),
                )
            )
            continue

        best_overlap = 0.0
        for chunk_id in evaluated_chunk_ids:
            evidence_tokens = _tokenize(chunk_text_by_id[chunk_id])
            if not evidence_tokens:
                continue
            overlap = len(claim_tokens & evidence_tokens) / max(1, len(claim_tokens))
            if overlap > best_overlap:
                best_overlap = overlap

        if best_overlap >= 0.55:
            status = "supported"
        elif best_overlap >= 0.25:
            status = "uncertain"
        else:
            status = "unsupported"

        results.append(
            ClaimCheckResult(
                claim_id=claim.claim_id,
                status=status,
                score=round(best_overlap, 4),
                evaluated_chunk_ids=evaluated_chunk_ids,
            )
        )

    return results
