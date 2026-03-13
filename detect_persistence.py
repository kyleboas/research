import hashlib
import logging
import re

from detect_policy import compute_final_score

log = logging.getLogger("research")


def normalize_trend_text(trend: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (trend or "").lower()).split())


def trend_fingerprint(trend: str) -> str:
    normalized = normalize_trend_text(trend)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def upsert_trend_candidate(conn, candidate: dict, feedback_adjustment: int):
    fingerprint = trend_fingerprint(candidate["trend"])
    base_score = int(candidate["score"])
    novelty = candidate.get("novelty_score")
    source_diversity = candidate.get("source_diversity", len(candidate.get("sources") or []))
    
    # Trajectory fields
    velocity = candidate.get("velocity_score")
    acceleration = candidate.get("acceleration_score")
    direction = candidate.get("trajectory_direction")
    early_trend = candidate.get("early_trend_score")
    trajectory_reasoning = candidate.get("trajectory_reasoning")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, feedback_adjustment, score, source_diversity FROM trend_candidates WHERE trend_fingerprint = %s LIMIT 1",
            (fingerprint,),
        )
        existing = cur.fetchone()
        if existing:
            candidate_id, existing_status, existing_feedback, existing_score, existing_source_diversity = existing
            stored_score = max(existing_score or 0, base_score)
            stored_diversity = max(existing_source_diversity or 0, source_diversity)
            stored_feedback = existing_feedback if existing_feedback not in (None, 0) else feedback_adjustment
            final_score = compute_final_score(
                base_score=stored_score,
                novelty_score=novelty,
                feedback_adjustment=stored_feedback,
                source_diversity=stored_diversity,
            )
            next_status = "reported" if existing_status == "reported" else "pending"
            cur.execute(
                """
                UPDATE trend_candidates
                SET trend = %s,
                    reasoning = %s,
                    score = %s,
                    feedback_adjustment = %s,
                    final_score = %s,
                    novelty_score = %s,
                    source_diversity = %s,
                    status = %s,
                    detected_at = NOW(),
                    velocity_score = %s,
                    acceleration_score = %s,
                    trajectory_direction = %s,
                    early_trend_score = %s,
                    trajectory_reasoning = %s
                WHERE id = %s
                RETURNING id, final_score
                """,
                (
                    candidate["trend"],
                    candidate.get("reasoning"),
                    stored_score,
                    stored_feedback,
                    final_score,
                    novelty,
                    stored_diversity,
                    next_status,
                    velocity,
                    acceleration,
                    direction,
                    early_trend,
                    trajectory_reasoning,
                    candidate_id,
                ),
            )
            row = cur.fetchone()
            return row[0], int(row[1] or final_score), stored_diversity

        final_score = compute_final_score(
            base_score=base_score,
            novelty_score=novelty,
            feedback_adjustment=feedback_adjustment,
            source_diversity=source_diversity,
        )
        cur.execute(
            """
            INSERT INTO trend_candidates
            (trend_fingerprint, trend, reasoning, score, feedback_adjustment, final_score, 
             novelty_score, source_diversity, velocity_score, acceleration_score, 
             trajectory_direction, early_trend_score, trajectory_reasoning)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, final_score
            """,
            (
                fingerprint,
                candidate["trend"],
                candidate.get("reasoning"),
                base_score,
                feedback_adjustment,
                final_score,
                novelty,
                source_diversity,
                velocity,
                acceleration,
                direction,
                early_trend,
                trajectory_reasoning,
            ),
        )
        row = cur.fetchone()
        return row[0], int(row[1] or final_score), source_diversity


def effective_source_diversity(stored_source_diversity: int | None, linked_source_count: int | None) -> int:
    return max(int(stored_source_diversity or 0), int(linked_source_count or 0))


def rescored_trend_candidate_values(
    *,
    base_score: int,
    feedback_adjustment: int,
    stored_source_diversity: int | None,
    linked_source_count: int | None,
    novelty_score: float | None,
) -> tuple[int, int]:
    source_diversity = effective_source_diversity(stored_source_diversity, linked_source_count)
    final_score = compute_final_score(
        base_score=int(base_score),
        novelty_score=novelty_score,
        feedback_adjustment=int(feedback_adjustment or 0),
        source_diversity=source_diversity,
    )
    return source_diversity, final_score


def parse_rescore_statuses(raw: str | None) -> list[str] | None:
    statuses = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    return statuses or None


def persist_detect_candidates(conn, candidates: list[dict]) -> list[int]:
    stored_scores: list[int] = []
    with conn.cursor() as cur:
        for candidate in candidates:
            trend_candidate_id, final_score, _ = upsert_trend_candidate(
                conn,
                candidate,
                int(candidate.get("feedback_adjustment", 0)),
            )
            for source in candidate.get("sources") or []:
                cur.execute(
                    "INSERT INTO trend_candidate_sources (trend_candidate_id, source_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (trend_candidate_id, source["source_id"]),
                )
            stored_scores.append(final_score)
    return stored_scores


def load_rescore_candidates(conn, *, limit: int = 0, statuses: list[str] | None = None):
    query = """
        SELECT
            tc.id,
            tc.trend,
            tc.score,
            tc.feedback_adjustment,
            COALESCE(tc.source_diversity, 0) AS stored_source_diversity,
            COUNT(tcs.source_id) AS linked_source_count,
            tc.novelty_score,
            COALESCE(tc.final_score, tc.score) AS existing_final_score,
            tc.status
        FROM trend_candidates tc
        LEFT JOIN trend_candidate_sources tcs ON tcs.trend_candidate_id = tc.id
    """
    params = []
    if statuses:
        placeholders = ",".join(["%s"] * len(statuses))
        query += f" WHERE tc.status IN ({placeholders})"
        params.extend(statuses)
    query += """
        GROUP BY tc.id
        ORDER BY tc.detected_at DESC, tc.id DESC
    """
    if limit and limit > 0:
        query += " LIMIT %s"
        params.append(int(limit))

    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def update_rescored_candidates(conn, updates: list[tuple]) -> int:
    changed = 0
    with conn.cursor() as cur:
        for (
            candidate_id,
            novelty_score,
            final_score,
            source_diversity,
            existing_novelty,
            existing_final_score,
            stored_source_diversity,
            status,
        ) in updates:
            cur.execute(
                """
                UPDATE trend_candidates
                SET novelty_score = %s,
                    final_score = %s,
                    source_diversity = %s
                WHERE id = %s
                """,
                (novelty_score, final_score, source_diversity, candidate_id),
            )
            if (
                existing_novelty is None
                or abs(float(existing_novelty) - float(novelty_score)) > 1e-6
                or int(existing_final_score) != int(final_score)
                or int(stored_source_diversity) != int(source_diversity)
            ):
                changed += 1
            log.debug(
                "Rescored trend_candidate id=%s status=%s final_score=%s novelty=%.4f source_diversity=%s",
                candidate_id,
                status,
                final_score,
                novelty_score,
                source_diversity,
            )
    return changed
