import logging
import math
import re
from datetime import UTC, datetime

from novelty_scoring import compute_novelty_score

log = logging.getLogger("research")


def tokenize_feedback_text(text: str) -> list[str]:
    words = [token for token in re.findall(r"[a-z0-9']+", text.lower()) if len(token) > 2]
    bigrams = [f"{words[idx]}_{words[idx + 1]}" for idx in range(len(words) - 1)]
    return words + bigrams


def load_feedback_keyword_weights(conn) -> dict[str, float]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trend_text, feedback_value, created_at FROM trend_feedback ORDER BY created_at DESC LIMIT 2000"
        )
        rows = cur.fetchall()

    if not rows:
        return {}

    now = datetime.now(UTC) if hasattr(datetime, "now") else datetime.utcnow().replace(tzinfo=UTC)
    half_life_days = 14.0
    decay_k = 0.693 / half_life_days

    weights: dict[str, float] = {}
    for trend_text, feedback, created_at in rows:
        if not trend_text or not feedback:
            continue
        if created_at and hasattr(created_at, "timestamp"):
            age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
        else:
            age_days = 0.0
        time_weight = math.exp(-decay_k * age_days)

        for token in set(tokenize_feedback_text(trend_text)):
            weights[token] = weights.get(token, 0.0) + float(feedback) * time_weight

    return weights


def load_feedback_embeddings(conn, *, embed_fn) -> list[tuple[list[float], int]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (trend_text) trend_text, feedback_value
               FROM trend_feedback
               ORDER BY trend_text, created_at DESC
               LIMIT 200"""
        )
        rows = cur.fetchall()

    if not rows:
        return []

    texts = [row[0] for row in rows if row[0]]
    feedbacks = [int(row[1]) for row in rows if row[0]]
    if not texts:
        return []

    vectors = embed_fn(texts)
    if not vectors:
        return []

    return list(zip(vectors, feedbacks))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def feedback_adjustment_for_trend(
    trend: str,
    keyword_weights: dict[str, float],
    feedback_embeddings: list[tuple[list[float], int]] | None = None,
    *,
    embed_fn,
) -> int:
    adjustment = 0.0

    if keyword_weights:
        for token in set(tokenize_feedback_text(trend or "")):
            weight = keyword_weights.get(token, 0.0)
            if "_" in token:
                weight *= 2.0
            adjustment += weight

    if feedback_embeddings and trend:
        trend_vectors = embed_fn([trend])
        if trend_vectors:
            trend_vec = trend_vectors[0]
            semantic_adj = 0.0
            for fb_vec, fb_value in feedback_embeddings:
                sim = cosine_similarity(trend_vec, fb_vec)
                if sim > 0.6:
                    strength = (sim - 0.6) / 0.4
                    semantic_adj += strength * fb_value
            adjustment += max(-25.0, min(25.0, semantic_adj))

    return max(-50, min(50, int(round(adjustment))))


def enrich_candidates_with_novelty(conn, candidates: list[dict], *, embed_fn) -> None:
    candidates_needing_novelty = [candidate for candidate in candidates if "novelty_score" not in candidate]
    if not candidates_needing_novelty:
        return

    novelty_texts = [candidate["trend"] for candidate in candidates_needing_novelty]
    novelty_vecs = embed_fn(novelty_texts)
    if not novelty_vecs:
        return

    for candidate, vec in zip(candidates_needing_novelty, novelty_vecs):
        if not vec:
            continue
        src_count = len(candidate.get("sources") or [])
        candidate["novelty_score"] = compute_novelty_score(
            conn,
            candidate["trend"],
            vec,
            source_count=src_count,
        )
        candidate["_embedding"] = vec
