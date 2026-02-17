"""Verification scoring and summary statistics."""

from __future__ import annotations

from dataclasses import dataclass

from .nli_check import ClaimCheckResult


@dataclass(frozen=True)
class VerificationScore:
    total_claims: int
    supported_claims: int
    uncertain_claims: int
    unsupported_claims: int
    quality_score: float


def score_claim_results(results: list[ClaimCheckResult]) -> VerificationScore:
    total = len(results)
    supported = sum(1 for result in results if result.status == "supported")
    uncertain = sum(1 for result in results if result.status == "uncertain")
    unsupported = sum(1 for result in results if result.status == "unsupported")

    if total == 0:
        return VerificationScore(
            total_claims=0,
            supported_claims=0,
            uncertain_claims=0,
            unsupported_claims=0,
            quality_score=0.0,
        )

    weighted_points = supported + (0.5 * uncertain)
    quality_score = round((weighted_points / total) * 100.0, 2)

    return VerificationScore(
        total_claims=total,
        supported_claims=supported,
        uncertain_claims=uncertain,
        unsupported_claims=unsupported,
        quality_score=quality_score,
    )
