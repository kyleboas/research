#!/usr/bin/env python3
"""Legacy hand-crafted policy optimization for ingest tuning.

Original implementation using 4 manually designed policy variants.
Kept for backward compatibility and comparison purposes.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db_conn import resolve_database_conninfo
from ingest_policy import get_policy_path, load_policy, save_policy

RESULTS_PATH = Path(__file__).resolve().parent / "results.tsv"
DEFAULT_MIN_IMPROVEMENT = 1.0


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    index = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return float(ordered[index])


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def candidate_policies(base_policy: dict):
    """Generate 4 hand-crafted policy variants."""
    rss = int(base_policy["rss_overlap_seconds"])
    yt = int(base_policy["youtube_overlap_seconds"])
    detect_min = int(base_policy["detect_min_new_sources"])
    candidates = [
        dict(base_policy),
        {
            **base_policy,
            "rss_overlap_seconds": max(6 * 60 * 60, rss // 2),
            "youtube_overlap_seconds": max(6 * 60 * 60, yt // 2),
            "detect_min_new_sources": min(3, detect_min + 1),
        },
        {
            **base_policy,
            "rss_overlap_seconds": min(72 * 60 * 60, max(rss, 24 * 60 * 60)),
            "youtube_overlap_seconds": min(72 * 60 * 60, max(yt, 24 * 60 * 60)),
            "detect_min_new_sources": max(0, detect_min - 1),
        },
        {
            **base_policy,
            "rss_overlap_seconds": 24 * 60 * 60,
            "youtube_overlap_seconds": 24 * 60 * 60,
            "detect_min_new_sources": 2,
        },
        {
            **base_policy,
            "rss_overlap_seconds": 48 * 60 * 60,
            "youtube_overlap_seconds": 48 * 60 * 60,
            "detect_min_new_sources": 5,
        },
    ]
    seen = set()
    for policy in candidates:
        key = json.dumps(policy, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        yield policy


def load_ingest_observations(conn, *, lookback_days: int = 30) -> dict:
    """Load historical source lag and volume data."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                source_type,
                EXTRACT(EPOCH FROM (created_at - (publish_date::timestamp))) / 3600.0 AS lag_hours
            FROM sources
            WHERE created_at >= NOW() - (%s || ' days')::interval
              AND publish_date IS NOT NULL
            """,
            (int(lookback_days),),
        )
        lag_rows = cur.fetchall()

        cur.execute(
            """
            SELECT DATE(created_at) AS day, COUNT(*)
            FROM sources
            WHERE created_at >= NOW() - (%s || ' days')::interval
            GROUP BY DATE(created_at)
            ORDER BY day DESC
            """,
            (int(lookback_days),),
        )
        daily_rows = cur.fetchall()

    rss_lags = [max(0.0, float(row[1] or 0.0)) for row in lag_rows if (row[0] or "") == "rss"]
    youtube_lags = [max(0.0, float(row[1] or 0.0)) for row in lag_rows if (row[0] or "") == "youtube"]
    daily_counts = [int(row[1] or 0) for row in daily_rows]
    avg_daily_sources = (sum(daily_counts) / len(daily_counts)) if daily_counts else 0.0

    return {
        "rss_p90_lag_hours": percentile(rss_lags, 90),
        "youtube_p90_lag_hours": percentile(youtube_lags, 90),
        "avg_daily_sources": avg_daily_sources,
        "sample_days": len(daily_counts),
    }


def score_policy(policy: dict, observations: dict) -> float:
    """Score a policy based on how well it matches observed patterns."""
    rss_overlap_hours = int(policy["rss_overlap_seconds"]) / 3600.0
    youtube_overlap_hours = int(policy["youtube_overlap_seconds"]) / 3600.0
    detect_min = int(policy["detect_min_new_sources"])

    rss_target = clamp(float(observations.get("rss_p90_lag_hours", 24.0) or 24.0), 6.0, 72.0)
    youtube_target = clamp(float(observations.get("youtube_p90_lag_hours", 24.0) or 24.0), 6.0, 72.0)
    avg_daily_sources = float(observations.get("avg_daily_sources", 0.0) or 0.0)
    detect_target = int(clamp(round(avg_daily_sources / 8.0), 0, 8))

    coverage_penalty = abs(rss_overlap_hours - rss_target) * 0.9 + abs(youtube_overlap_hours - youtube_target) * 0.7
    efficiency_penalty = max(0.0, rss_overlap_hours - 48.0) * 0.15 + max(0.0, youtube_overlap_hours - 48.0) * 0.15
    detect_penalty = abs(detect_min - detect_target) * 3.0

    return round(100.0 - coverage_penalty - efficiency_penalty - detect_penalty, 2)


def policy_changed(base_policy: dict, candidate_policy: dict) -> bool:
    return json.dumps(base_policy, sort_keys=True) != json.dumps(candidate_policy, sort_keys=True)


def apply_decision(*, baseline_score: float, best_score: float, min_improvement: float, changed: bool) -> tuple[bool, str]:
    if not changed:
        return False, "no_policy_change"
    if float(best_score) - float(baseline_score) < float(min_improvement):
        return False, "below_min_improvement"
    return True, "applied"


def append_result_row(result: dict):
    """Append optimization result to results.tsv."""
    header = "timestamp\tbaseline\tbest\tdelta\tapplied\tapply_decision\tpolicy_json\n"
    if not RESULTS_PATH.exists() or not RESULTS_PATH.read_text().startswith(header):
        RESULTS_PATH.write_text(header)
    row = "\t".join(
        [
            datetime.now(UTC).isoformat(),
            f"{float(result['baseline_score']):.2f}",
            f"{float(result['best_score']):.2f}",
            f"{float(result['delta']):.2f}",
            "yes" if result.get("applied") else "no",
            str(result.get("apply_decision") or ""),
            json.dumps(result["best_policy"], sort_keys=True),
        ]
    )
    with RESULTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(row + "\n")


def ensure_ingest_policy_runs_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ingest_policy_runs (
                id BIGSERIAL PRIMARY KEY,
                baseline_score DOUBLE PRECISION NOT NULL,
                best_score DOUBLE PRECISION NOT NULL,
                delta DOUBLE PRECISION NOT NULL,
                min_improvement DOUBLE PRECISION NOT NULL DEFAULT 0,
                applied BOOLEAN NOT NULL DEFAULT FALSE,
                apply_decision TEXT NOT NULL DEFAULT '',
                observations JSONB NOT NULL DEFAULT '{}'::jsonb,
                baseline_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
                best_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ingest_policy_runs_created_at
            ON ingest_policy_runs (created_at DESC)
            """
        )


def save_pipeline_state(conn, key: str, value: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_state (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = NOW()
            """,
            (key, str(value)),
        )


def record_run(conn, *, baseline_policy: dict, best_policy: dict, observations: dict, baseline_score: float, best_score: float, min_improvement: float, applied: bool, apply_decision_value: str):
    ensure_ingest_policy_runs_table(conn)
    delta = float(best_score) - float(baseline_score)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingest_policy_runs (
                baseline_score,
                best_score,
                delta,
                min_improvement,
                applied,
                apply_decision,
                observations,
                baseline_policy,
                best_policy
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
            """,
            (
                float(baseline_score),
                float(best_score),
                delta,
                float(min_improvement),
                bool(applied),
                apply_decision_value,
                json.dumps(observations, sort_keys=True),
                json.dumps(baseline_policy, sort_keys=True),
                json.dumps(best_policy, sort_keys=True),
            ),
        )
    save_pipeline_state(conn, "last_ingest_policy_baseline", f"{float(baseline_score):.2f}")
    save_pipeline_state(conn, "last_ingest_policy_best", f"{float(best_score):.2f}")
    save_pipeline_state(conn, "last_ingest_policy_delta", f"{delta:.2f}")
    save_pipeline_state(conn, "last_ingest_policy_applied", "yes" if applied else "no")
    save_pipeline_state(conn, "last_ingest_policy_apply_decision", apply_decision_value)


def main():
    parser = argparse.ArgumentParser(description="[LEGACY] Optimize ingest policy with hand-crafted variants")
    parser.add_argument("--lookback-days", type=int, default=30, help="How many recent days of sources to analyze")
    parser.add_argument("--apply", action="store_true", help="Write the best ingest policy to ingest_policy_config.json")
    parser.add_argument("--min-improvement", type=float, default=DEFAULT_MIN_IMPROVEMENT, help="Minimum score improvement required")
    args = parser.parse_args()

    conninfo, reason = resolve_database_conninfo()
    if not conninfo:
        raise SystemExit(f"database_unavailable:{reason}")

    base_policy = load_policy()
    with psycopg.connect(conninfo) as conn:
        observations = load_ingest_observations(conn, lookback_days=args.lookback_days)
        baseline_score = score_policy(base_policy, observations)
        best_policy = dict(base_policy)
        best_score = baseline_score
        for candidate in candidate_policies(base_policy):
            candidate_score = score_policy(candidate, observations)
            if candidate_score > best_score:
                best_policy = dict(candidate)
                best_score = candidate_score

        changed = policy_changed(base_policy, best_policy)
        applied, apply_decision_value = apply_decision(
            baseline_score=baseline_score,
            best_score=best_score,
            min_improvement=float(args.min_improvement),
            changed=changed,
        )
        if args.apply and applied:
            saved_path = save_policy(best_policy)
        else:
            saved_path = None

        record_run(
            conn,
            baseline_policy=base_policy,
            best_policy=best_policy,
            observations=observations,
            baseline_score=baseline_score,
            best_score=best_score,
            min_improvement=float(args.min_improvement),
            applied=bool(args.apply and applied),
            apply_decision_value=apply_decision_value if args.apply else "not_requested",
        )
        conn.commit()

    result = {
        "baseline_score": baseline_score,
        "best_score": best_score,
        "delta": round(best_score - baseline_score, 2),
        "applied": bool(args.apply and applied),
        "apply_decision": apply_decision_value if args.apply else "not_requested",
        "best_policy": best_policy,
    }
    append_result_row(result)

    print(f"policy_path={get_policy_path().resolve()}")
    print("observations=" + json.dumps(observations, sort_keys=True))
    print(f"baseline={baseline_score:.2f}")
    print(f"best={best_score:.2f}")
    print(f"delta={best_score - baseline_score:.2f}")
    print(f"min_improvement={float(args.min_improvement):.2f}")
    print(f"policy_changed={'yes' if changed else 'no'}")
    print(f"apply_decision={result['apply_decision']}")
    print("best_policy=" + json.dumps(best_policy, sort_keys=True))
    if saved_path is not None:
        print(f"applied_policy={saved_path.resolve()}")


if __name__ == "__main__":
    main()
