import logging

from novelty_scoring import compute_novelty_score

from detect_persistence import (
    effective_source_diversity,
    load_rescore_candidates,
    persist_detect_candidates,
    rescored_trend_candidate_values,
    update_rescored_candidates,
)
from detect_scoring import (
    enrich_candidates_with_novelty,
    feedback_adjustment_for_trend,
    load_feedback_embeddings,
    load_feedback_keyword_weights,
)
from detect_trajectory import TrajectoryAnalyzer, batch_analyze_trajectories, filter_early_trends

log = logging.getLogger("research")


def run_detect(
    conn,
    *,
    min_new_sources: int = 0,
    backfill_days: int,
    backfill_limit: int = 200,
    load_state_fn,
    count_recent_embedded_chunks_fn,
    run_backfill_fn,
    detect_trends_fn,
    embed_fn,
    trajectory_analyzer: TrajectoryAnalyzer | None = None,
    early_trend_mode: bool = True,
):
    if min_new_sources > 0:
        latest_new = load_state_fn(conn, "last_ingest_new_sources")
        try:
            latest_new_count = int(latest_new) if latest_new is not None else 0
        except ValueError:
            latest_new_count = 0
        if latest_new_count < min_new_sources:
            log.info(
                "Skipping detect: only %d new sources in latest ingest (min required: %d)",
                latest_new_count,
                min_new_sources,
            )
            return

    recent_embedded_chunks = count_recent_embedded_chunks_fn(conn, backfill_days)
    if recent_embedded_chunks == 0:
        log.warning(
            "Detect found 0 recent embedded chunks for BERTrend lookback=%dd. Triggering backfill.",
            backfill_days,
        )
        run_backfill_fn(conn, lookback_days=backfill_days, limit=backfill_limit)

    candidates, had_error = detect_trends_fn(conn)
    if had_error:
        log.error(
            "Trend detection run failed due to response-format/parsing error (candidates returned: %d)",
            len(candidates),
        )
        raise SystemExit(1)

    if not candidates:
        log.info("No novel trends detected this run")
        return

    keyword_weights = load_feedback_keyword_weights(conn)
    feedback_embeddings = load_feedback_embeddings(conn, embed_fn=embed_fn)
    if feedback_embeddings:
        log.info("Loaded %d feedback embeddings for semantic matching", len(feedback_embeddings))

    enrich_candidates_with_novelty(conn, candidates, embed_fn=embed_fn)
    for candidate in candidates:
        candidate["feedback_adjustment"] = feedback_adjustment_for_trend(
            candidate["trend"],
            keyword_weights,
            feedback_embeddings,
            embed_fn=embed_fn,
        )

    # Trajectory analysis for early-trend detection
    if trajectory_analyzer is None:
        trajectory_analyzer = TrajectoryAnalyzer()
    
    candidates = batch_analyze_trajectories(conn, candidates, analyzer=trajectory_analyzer)
    
    # Count rising vs falling trends
    rising_count = sum(1 for c in candidates if c.get("trajectory_direction") == "rising")
    falling_count = sum(1 for c in candidates if c.get("trajectory_direction") == "falling")
    flat_count = sum(1 for c in candidates if c.get("trajectory_direction") == "flat")
    log.info(
        "Trajectory analysis: %d rising, %d falling, %d flat trajectories",
        rising_count, falling_count, flat_count
    )
    
    # In early-trend mode, filter to focus on rising trends with good early-trend scores
    if early_trend_mode:
        early_trends = filter_early_trends(candidates, min_early_trend_score=0.5, require_rising=True)
        if early_trends:
            log.info(
                "Early-trend mode: filtered %d candidates to %d early-trends (top score: %.2f)",
                len(candidates), len(early_trends), 
                max(c.get("early_trend_score", 0) for c in early_trends)
            )
            candidates = early_trends
        else:
            log.info("Early-trend mode: no candidates meet early-trend criteria, keeping all")

    stored_scores = persist_detect_candidates(conn, candidates)
    conn.commit()
    log.info("Stored %d trend candidates (top final score: %d)", len(candidates), max(stored_scores))


def run_rescore(conn, *, limit: int = 0, batch_size: int = 100, statuses: list[str] | None = None, embed_fn):
    rows = load_rescore_candidates(conn, limit=limit, statuses=statuses)
    if not rows:
        log.info("Trend rescore skipped: no trend candidates matched the requested filters")
        return 0

    batch_size = max(1, int(batch_size or 1))
    log.info(
        "Trend rescore starting: %d candidates (statuses=%s, batch_size=%d)",
        len(rows),
        ",".join(statuses) if statuses else "all",
        batch_size,
    )

    processed = 0
    changed = 0
    skipped = 0

    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        vectors = embed_fn([row[1] for row in batch])
        if not vectors:
            log.error("Trend rescore aborted: embedding call failed for batch starting at offset %d", start)
            raise SystemExit(1)

        updates = []
        for row, vec in zip(batch, vectors):
            (
                candidate_id,
                trend,
                base_score,
                feedback_adjustment,
                stored_source_diversity,
                linked_source_count,
                existing_novelty,
                existing_final_score,
                status,
            ) = row
            if not vec:
                skipped += 1
                log.warning("Trend rescore skipped candidate_id=%s because embedding was unavailable", candidate_id)
                continue

            source_diversity = effective_source_diversity(stored_source_diversity, linked_source_count)
            novelty_score = compute_novelty_score(conn, trend, vec, source_count=source_diversity)
            source_diversity, final_score = rescored_trend_candidate_values(
                base_score=base_score,
                feedback_adjustment=feedback_adjustment,
                stored_source_diversity=stored_source_diversity,
                linked_source_count=linked_source_count,
                novelty_score=novelty_score,
            )
            updates.append(
                (
                    candidate_id,
                    novelty_score,
                    final_score,
                    source_diversity,
                    existing_novelty,
                    int(existing_final_score or base_score),
                    int(stored_source_diversity or 0),
                    status,
                )
            )

        processed += len(updates)
        changed += update_rescored_candidates(conn, updates)
        conn.commit()
        log.info(
            "Trend rescore progress: %d/%d processed (%d changed, %d skipped)",
            min(start + len(batch), len(rows)),
            len(rows),
            changed,
            skipped,
        )

    log.info(
        "Trend rescore complete: %d processed, %d changed, %d skipped",
        processed,
        changed,
        skipped,
    )
    return processed
