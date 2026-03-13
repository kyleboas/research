"""Novelty scoring for trend candidates.

Scores candidates by comparing them against the historical corpus of previously
seen tactical concepts. A candidate is scored higher when it is semantically
distant from what has been seen before but still supported by multiple sources.

Two complementary signals:
  1. Semantic novelty: cosine distance from nearest historical baseline embeddings
  2. Source diversity: how many independent sources mention the pattern
     (few sources = early/niche, many sources = already mainstream)

The ideal candidate for early tactical detection:
  - Semantically novel (high distance from historical baselines)
  - Supported by 2-5 independent, high-quality sources (not just one outlier)
  - Not yet broadly adopted (appears in specific teams/coaches, not everywhere)
"""

import logging
import math
import re
from datetime import UTC, datetime

import numpy as np

log = logging.getLogger("research")

_WORD_RE = re.compile(r"[a-z]+(?:-[a-z]+)?")
_TACTICAL_TERMS = {
    "back",
    "backs",
    "build-up",
    "counterpress",
    "counter-press",
    "cross",
    "crosses",
    "corner",
    "corners",
    "defender",
    "defenders",
    "full-back",
    "fullbacks",
    "goal-kick",
    "goal-kicks",
    "half-space",
    "half-spaces",
    "inverts",
    "midfield",
    "midfielder",
    "midfielders",
    "overload",
    "overloads",
    "press",
    "pressing",
    "set-piece",
    "set-pieces",
    "striker",
    "wing-back",
    "wingbacks",
    "winger",
    "wingers",
    "zone",
    "zones",
}
_GENERIC_TERMS = {
    "adopting",
    "analytics",
    "approach",
    "development",
    "direct",
    "expertise",
    "focusing",
    "formation",
    "improving",
    "innovation",
    "leveraging",
    "management",
    "method",
    "methods",
    "model",
    "models",
    "optimizing",
    "philosophy",
    "player",
    "players",
    "prioritizing",
    "process",
    "processes",
    "recruitment",
    "strategy",
    "style",
    "success",
    "system",
    "systems",
    "talent",
    "team",
    "teams",
    "technology",
    "using",
}


def _clamp_unit(value):
    return max(0.0, min(1.0, float(value)))


def _weighted_log_average(rows, index):
    weighted_total = 0.0
    weight_sum = 0.0
    for row in rows:
        similarity = _clamp_unit(row[1])
        weight = max(0.05, similarity ** 2)
        weighted_total += math.log1p(max(0, row[index] or 0)) * weight
        weight_sum += weight
    if weight_sum <= 0:
        return 0.0
    return weighted_total / weight_sum


def _recency_penalty(rows):
    now = datetime.now(UTC)
    freshest_days = None
    for row in rows:
        similarity = _clamp_unit(row[1])
        last_seen = row[4] if len(row) > 4 else None
        if similarity < 0.72 or last_seen is None:
            continue
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        age_days = max(0.0, (now - last_seen).total_seconds() / 86400.0)
        if freshest_days is None or age_days < freshest_days:
            freshest_days = age_days

    if freshest_days is None:
        return 0.0
    if freshest_days <= 7:
        return 0.18
    if freshest_days <= 30:
        return 0.12
    if freshest_days <= 90:
        return 0.06
    return 0.0


def _specificity_penalty(trend_text):
    tokens = _WORD_RE.findall((trend_text or "").lower())
    if not tokens:
        return 0.0

    token_set = set(tokens)
    tactical_hits = sum(1 for token in token_set if token in _TACTICAL_TERMS)
    generic_hits = sum(1 for token in token_set if token in _GENERIC_TERMS)
    generic_ratio = generic_hits / max(1, len(token_set))

    if tactical_hits == 0 and generic_hits >= 2 and generic_ratio >= 0.34:
        return 0.18
    if tactical_hits <= 1 and generic_hits >= 2 and generic_ratio >= 0.28:
        return 0.1
    if len(tokens) <= 4 and tactical_hits == 0:
        return 0.06
    return 0.0


def compute_novelty_score(conn, trend_text, trend_embedding, source_count=1):
    """Compute novelty score for a trend candidate.

    Args:
        conn: Database connection
        trend_text: The trend description text
        trend_embedding: Pre-computed embedding vector for the trend
        source_count: Number of independent sources supporting this trend

    Returns:
        float: Novelty score 0.0-1.0 where 1.0 = completely novel
    """
    if not trend_embedding:
        return 0.5  # neutral if we can't compute

    vec_literal = "[" + ",".join(str(v) for v in trend_embedding) + "]"

    # Find nearest historical baselines
    with conn.cursor() as cur:
        cur.execute(
            "SELECT concept, 1 - (embedding <=> %s::vector) AS similarity, "
            "occurrence_count, source_count, last_seen "
            "FROM novelty_baselines "
            "ORDER BY embedding <=> %s::vector "
            "LIMIT 5",
            (vec_literal, vec_literal),
        )
        nearest = cur.fetchall()

    if not nearest:
        # No historical baselines at all — everything is novel
        return 0.95

    # Semantic novelty: inverse of max similarity to historical concepts
    max_similarity = max(_clamp_unit(row[1]) for row in nearest)
    semantic_novelty = 1.0 - max_similarity

    closest = nearest[0]
    closest_occurrences = max(1, closest[2] or 1)

    # Penalize trends that live in a crowded, well-established neighborhood.
    prevalence_penalty = min(
        0.35,
        _weighted_log_average(nearest, 2) * 0.05 + _weighted_log_average(nearest, 3) * 0.035,
    )

    # If several nearby concepts are already clustered around this idea, treat it
    # as a mainstream theme rather than a genuinely new concept.
    avg_similarity = sum(_clamp_unit(row[1]) for row in nearest) / len(nearest)
    neighborhood_penalty = min(0.16, max(0.0, avg_similarity - 0.55) * 0.35)
    recency_penalty = _recency_penalty(nearest)
    specificity_penalty = _specificity_penalty(trend_text)

    # Source diversity signal
    # Sweet spot: 2-5 sources = early but supported. 1 source = outlier risk.
    # >10 sources = probably already mainstream.
    if source_count <= 1:
        diversity_bonus = -0.1  # single source = higher risk of noise
    elif source_count <= 5:
        diversity_bonus = 0.15  # sweet spot: early but corroborated
    elif source_count <= 10:
        diversity_bonus = 0.05  # getting mainstream
    else:
        diversity_bonus = -0.1  # widely covered = not novel

    novelty = max(
        0.0,
        min(
            1.0,
            semantic_novelty
            - prevalence_penalty
            - neighborhood_penalty
            - recency_penalty
            - specificity_penalty
            + diversity_bonus,
        ),
    )

    log.info(
        "Novelty score for '%s': %.3f (semantic=%.3f, prevalence=%.3f, "
        "neighborhood=%.3f, recency=%.3f, specificity=%.3f, diversity=%.3f, "
        "closest='%s' sim=%.3f seen=%d times)",
        trend_text[:60],
        novelty,
        semantic_novelty,
        prevalence_penalty,
        neighborhood_penalty,
        recency_penalty,
        specificity_penalty,
        diversity_bonus,
        closest[0][:40] if closest[0] else "?",
        max_similarity,
        closest_occurrences,
    )

    return round(novelty, 4)


def update_baseline(conn, trend_text, trend_embedding, source_count=1):
    """Add or update a concept in the novelty baseline corpus.

    Called after a trend candidate is processed (reported or evaluated),
    so future occurrences of similar concepts register as less novel.
    """
    if not trend_embedding:
        return

    vec_literal = "[" + ",".join(str(v) for v in trend_embedding) + "]"

    # Check if a similar concept already exists (cosine similarity > 0.85)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, concept, 1 - (embedding <=> %s::vector) AS similarity, "
            "occurrence_count, source_count "
            "FROM novelty_baselines "
            "WHERE 1 - (embedding <=> %s::vector) > 0.85 "
            "ORDER BY embedding <=> %s::vector "
            "LIMIT 1",
            (vec_literal, vec_literal, vec_literal),
        )
        existing = cur.fetchone()

    if existing:
        # Update existing baseline
        baseline_id = existing[0]
        new_occ = (existing[3] or 1) + 1
        new_src = max(existing[4] or 1, source_count)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE novelty_baselines SET "
                "occurrence_count = %s, source_count = %s, last_seen = NOW() "
                "WHERE id = %s",
                (new_occ, new_src, baseline_id),
            )
        log.debug("Updated novelty baseline #%d: '%s' (occurrences=%d)",
                  baseline_id, existing[1][:40], new_occ)
    else:
        # Insert new baseline
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO novelty_baselines (concept, embedding, source_count) "
                "VALUES (%s, %s::vector, %s)",
                (trend_text[:500], vec_literal, source_count),
            )
        log.debug("Added new novelty baseline: '%s'", trend_text[:60])


def score_tactical_pattern_novelty(conn, patterns, embed_fn):
    """Score novelty for a batch of tactical patterns.

    Takes extracted tactical patterns, embeds their action descriptions,
    and compares against the historical baseline to find genuinely new
    tactical behaviors.

    Args:
        conn: Database connection
        patterns: List of pattern dicts from tactical_extraction
        embed_fn: Function to compute embeddings (list[str] -> list[list[float]])

    Returns:
        List of (pattern, novelty_score) tuples, sorted by novelty descending
    """
    if not patterns:
        return []

    # Build concise descriptions for each pattern
    descriptions = []
    for p in patterns:
        desc = f"{p.get('actor', 'player')} {p['action']}"
        if p.get('zones'):
            desc += f" in {p['zones'][0]}"
        if p.get('phase'):
            desc += f" during {p['phase']}"
        descriptions.append(desc)

    # Batch embed
    vectors = embed_fn(descriptions)
    if not vectors:
        return [(p, 0.5) for p in patterns]

    # Score each pattern
    scored = []
    for pattern, desc, vec in zip(patterns, descriptions, vectors):
        novelty = compute_novelty_score(conn, desc, vec)
        scored.append((pattern, novelty))

    # Sort by novelty descending
    scored.sort(key=lambda x: -x[1])
    return scored
