#!/usr/bin/env python3
"""Bayesian optimization for ingest policy tuning.

Replaces hand-crafted policy variants with intelligent Bayesian optimization.
Uses historical source lag and volume data to optimize ingestion parameters.
Features:
- Systematic policy space exploration
- Budget-aware optimization (no expensive re-runs)
- Warm-start from previous results
- Parameter importance analysis
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
from autoresearch.bayesian_optimizer import (
    BayesianOptimizer,
    OptimizationConfig,
    OPTIMIZATION_PRESETS,
)

RESULTS_PATH = Path(__file__).resolve().parent / "results.tsv"
STUDY_STORAGE_PATH = Path(__file__).resolve().parent / ".study_cache.sqlite"
DEFAULT_MIN_IMPROVEMENT = 1.0

# Bayesian search space for ingest policy
SEARCH_SPACE_BAYESIAN = {
    "rss_overlap_seconds": ("int_step", 6 * 60 * 60, 96 * 60 * 60, 60 * 60),  # 6h to 96h in 1h steps
    "youtube_overlap_seconds": ("int_step", 6 * 60 * 60, 96 * 60 * 60, 60 * 60),
    "detect_min_new_sources": ("int_step", 0, 10, 1),
}


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    index = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return float(ordered[index])


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def build_policy_from_params(params: dict, base_policy: dict) -> dict:
    """Build complete policy from sampled parameters."""
    policy = dict(base_policy)
    policy.update(params)
    return policy


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
        "rss_samples": len(rss_lags),
        "youtube_samples": len(youtube_lags),
    }


def score_policy(policy: dict, observations: dict) -> float:
    """Score a policy based on how well it matches observed patterns.
    
    Higher score = better match to observed ingestion patterns.
    """
    rss_overlap_hours = int(policy["rss_overlap_seconds"]) / 3600.0
    youtube_overlap_hours = int(policy["youtube_overlap_seconds"]) / 3600.0
    detect_min = int(policy["detect_min_new_sources"])

    # Target values based on observations
    rss_target = clamp(float(observations.get("rss_p90_lag_hours", 24.0) or 24.0), 6.0, 72.0)
    youtube_target = clamp(float(observations.get("youtube_p90_lag_hours", 24.0) or 24.0), 6.0, 72.0)
    avg_daily_sources = float(observations.get("avg_daily_sources", 0.0) or 0.0)
    
    # Heuristic: detect_min should scale with daily volume
    # More sources per day = can afford to be more selective
    detect_target = int(clamp(round(avg_daily_sources / 8.0), 0, 8))

    # Coverage penalty: how well overlap matches observed lag
    coverage_penalty = (
        abs(rss_overlap_hours - rss_target) * 0.9 + 
        abs(youtube_overlap_hours - youtube_target) * 0.7
    )
    
    # Efficiency penalty: discourage overly long overlaps
    efficiency_penalty = (
        max(0.0, rss_overlap_hours - 48.0) * 0.15 + 
        max(0.0, youtube_overlap_hours - 48.0) * 0.15
    )
    
    # Detect penalty: how well detect_min matches target
    detect_penalty = abs(detect_min - detect_target) * 3.0

    # Score starts at 100 and penalties are subtracted
    return round(100.0 - coverage_penalty - efficiency_penalty - detect_penalty, 2)


def make_ingest_objective(observations: dict) -> callable:
    """Create objective function for ingest policy optimization."""
    
    def objective(trial, params: dict) -> float:
        policy = build_policy_from_params(params, {})
        
        # Primary score
        score = score_policy(policy, observations)
        
        # Penalize excessive overlap windows (cost inefficiency)
        rss_hours = int(policy["rss_overlap_seconds"]) / 3600.0
        youtube_hours = int(policy["youtube_overlap_seconds"]) / 3600.0
        
        # Small penalty for very long windows (>72h is excessive)
        if rss_hours > 72:
            score -= (rss_hours - 72) * 0.1
        if youtube_hours > 72:
            score -= (youtube_hours - 72) * 0.1
        
        # Reward matching RSS and YouTube targets separately
        rss_target = clamp(float(observations.get("rss_p90_lag_hours", 24.0) or 24.0), 6.0, 72.0)
        youtube_target = clamp(float(observations.get("youtube_p90_lag_hours", 24.0) or 24.0), 6.0, 72.0)
        
        rss_match = 1.0 - abs(rss_hours - rss_target) / 72.0
        youtube_match = 1.0 - abs(youtube_hours - youtube_target) / 72.0
        
        # Bonus for good matches
        score += rss_match * 2.0 + youtube_match * 2.0
        
        return score
    
    return objective


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
    header = "timestamp\tbaseline\tbest\tdelta\tapplied\tapply_decision\toptimization_type\tn_trials\tpolicy_json\n"
    
    if not RESULTS_PATH.exists() or not RESULTS_PATH.read_text().startswith(header):
        RESULTS_PATH.write_text(header)
    
    opt_type = result.get("optimization_type", "bayesian")
    n_trials = str(result.get("n_trials", ""))
    
    row = "\t".join(
        [
            datetime.now(UTC).isoformat(),
            f"{float(result['baseline_score']):.2f}",
            f"{float(result['best_score']):.2f}",
            f"{float(result['delta']):.2f}",
            "yes" if result.get("applied") else "no",
            str(result.get("apply_decision") or ""),
            opt_type,
            n_trials,
            json.dumps(result["best_policy"], sort_keys=True),
        ]
    )
    with RESULTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(row + "\n")


def ensure_ingest_policy_runs_table(conn):
    """Ensure the ingest_policy_runs table exists."""
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
                optimization_type TEXT NOT NULL DEFAULT 'bayesian',
                n_trials INTEGER NOT NULL DEFAULT 0,
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
        cur.execute(
            """
            ALTER TABLE ingest_policy_runs
            ADD COLUMN IF NOT EXISTS optimization_type TEXT NOT NULL DEFAULT 'bayesian'
            """
        )
        cur.execute(
            """
            ALTER TABLE ingest_policy_runs
            ADD COLUMN IF NOT EXISTS n_trials INTEGER NOT NULL DEFAULT 0
            """
        )
    if hasattr(conn, "commit"):
        conn.commit()


def get_ingest_policy_runs_columns(conn) -> set[str]:
    """Return the currently available columns for ingest_policy_runs."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'ingest_policy_runs'
            """
        )
        return {str(row[0]) for row in cur.fetchall()}


def save_pipeline_state(conn, key: str, value: str):
    """Save pipeline state to database."""
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


def record_run(
    conn,
    *,
    baseline_policy: dict,
    best_policy: dict,
    observations: dict,
    baseline_score: float,
    best_score: float,
    min_improvement: float,
    applied: bool,
    apply_decision_value: str,
    optimization_type: str = "bayesian",
    n_trials: int = 0,
):
    """Record optimization run to database."""
    ensure_ingest_policy_runs_table(conn)
    delta = float(best_score) - float(baseline_score)

    payload = {
        "baseline_score": float(baseline_score),
        "best_score": float(best_score),
        "delta": delta,
        "min_improvement": float(min_improvement),
        "applied": bool(applied),
        "apply_decision": apply_decision_value,
        "optimization_type": optimization_type,
        "n_trials": int(n_trials),
        "observations": json.dumps(observations, sort_keys=True),
        "baseline_policy": json.dumps(baseline_policy, sort_keys=True),
        "best_policy": json.dumps(best_policy, sort_keys=True),
    }
    jsonb_columns = {"observations", "baseline_policy", "best_policy"}
    available_columns = get_ingest_policy_runs_columns(conn)
    insert_columns = [column for column in payload if column in available_columns]
    if not insert_columns:
        raise RuntimeError("ingest_policy_runs has no writable columns")
    placeholders = [
        "%s::jsonb" if column in jsonb_columns else "%s"
        for column in insert_columns
    ]
    insert_sql = "INSERT INTO ingest_policy_runs ({}) VALUES ({})".format(
        ", ".join(insert_columns),
        ", ".join(placeholders),
    )

    with conn.cursor() as cur:
        cur.execute(
            insert_sql,
            tuple(payload[column] for column in insert_columns),
        )
    
    save_pipeline_state(conn, "last_ingest_policy_baseline", f"{float(baseline_score):.2f}")
    save_pipeline_state(conn, "last_ingest_policy_best", f"{float(best_score):.2f}")
    save_pipeline_state(conn, "last_ingest_policy_delta", f"{delta:.2f}")
    save_pipeline_state(conn, "last_ingest_policy_applied", "yes" if applied else "no")
    save_pipeline_state(conn, "last_ingest_policy_apply_decision", apply_decision_value)


def load_previous_results(results_path: Path) -> list[dict]:
    """Load previous optimization results for warm-start."""
    if not results_path.exists():
        return []
    
    try:
        content = results_path.read_text()
        lines = content.strip().split("\n")
        if len(lines) < 2:
            return []
        
        results = []
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) >= 9:
                try:
                    policy = json.loads(parts[8])  # policy_json column
                    best_score = float(parts[2])  # best score column
                    results.append({"params": policy, "value": best_score})
                except (json.JSONDecodeError, ValueError):
                    continue
        
        return results[-30:] if len(results) > 30 else results
    except Exception:
        return []


def main():
    parser = argparse.ArgumentParser(
        description="Bayesian optimization for ingest policy from historical observations"
    )
    parser.add_argument("--lookback-days", type=int, default=30, 
                        help="How many recent days of sources to analyze")
    parser.add_argument("--apply", action="store_true", 
                        help="Write the best ingest policy to ingest_policy_config.json")
    parser.add_argument("--min-improvement", type=float, default=DEFAULT_MIN_IMPROVEMENT,
                        help="Minimum score improvement required before applying")
    parser.add_argument("--trials", type=int, default=50, 
                        help="Number of optimization trials")
    parser.add_argument("--preset", choices=["fast", "thorough", "budget_constrained", "exploration"],
                        default="fast", help="Optimization preset")
    parser.add_argument("--timeout", type=float, help="Maximum optimization time in seconds")
    parser.add_argument("--warm-start", action="store_true", default=True,
                        help="Warm-start from previous results")
    parser.add_argument("--no-warm-start", dest="warm_start", action="store_false",
                        help="Disable warm-start")
    parser.add_argument("--study-name", help="Name for the Optuna study")
    parser.add_argument("--legacy", action="store_true", help="Use legacy hand-crafted policies")
    args = parser.parse_args()

    # Handle legacy mode
    if args.legacy:
        from autoresearch.ingest.optimize_ingest_policy_legacy import main as legacy_main
        legacy_main()
        return

    # Database connection
    conninfo, reason = resolve_database_conninfo()
    if not conninfo:
        raise SystemExit(f"database_unavailable:{reason}")

    base_policy = load_policy()
    
    with psycopg.connect(conninfo) as conn:
        observations = load_ingest_observations(conn, lookback_days=args.lookback_days)
        baseline_score = score_policy(base_policy, observations)
        
        # Setup optimization
        config = OPTIMIZATION_PRESETS[args.preset]
        if args.trials:
            config.n_trials = args.trials
        if args.timeout:
            config.timeout_seconds = args.timeout
        if args.study_name:
            config.study_name = args.study_name
        config.storage_path = str(STUDY_STORAGE_PATH)

        print(f"Starting Bayesian optimization with {config.n_trials} trials...")
        print(f"Preset: {args.preset}")
        print(f"Lookback: {args.lookback_days} days")
        print(f"Observations: {observations}")
        print(f"Search space: {len(SEARCH_SPACE_BAYESIAN)} parameters")
        
        # Create optimizer
        optimizer = BayesianOptimizer(config)
        optimizer.create_study(direction="maximize")
        
        # Warm-start
        if args.warm_start:
            previous_results = load_previous_results(RESULTS_PATH)
            if previous_results:
                print(f"Warm-starting with {len(previous_results)} previous results...")
                optimizer.warm_start_from_results(previous_results)
        
        # Create objective
        objective = make_ingest_objective(observations)
        
        # Run optimization
        try:
            result = optimizer.optimize(objective, SEARCH_SPACE_BAYESIAN)
        except ImportError as e:
            print(f"Error: {e}")
            print("Please install optuna: pip install optuna")
            raise SystemExit(1)
        
        # Build best policy
        best_params = result["best_params"]
        best_policy = build_policy_from_params(best_params, base_policy)
        best_score = score_policy(best_policy, observations)
        
        # Apply decision
        changed = policy_changed(base_policy, best_policy)
        applied, apply_decision_value = apply_decision(
            baseline_score=baseline_score,
            best_score=best_score,
            min_improvement=float(args.min_improvement),
            changed=changed,
        )
        
        saved_path = None
        if args.apply and applied:
            saved_path = save_policy(best_policy)

        # Record to database
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
            optimization_type="bayesian",
            n_trials=result["n_trials"],
        )
        conn.commit()

    # Append results
    result_record = {
        "baseline_score": baseline_score,
        "best_score": best_score,
        "delta": round(best_score - baseline_score, 2),
        "applied": bool(args.apply and applied),
        "apply_decision": apply_decision_value if args.apply else "not_requested",
        "best_policy": best_policy,
        "optimization_type": "bayesian",
        "n_trials": result["n_trials"],
    }
    append_result_row(result_record)

    # Get parameter importance
    importance = optimizer.get_importance()

    # Print results
    print(f"\n{'='*60}")
    print(f"Optimization Complete")
    print(f"{'='*60}")
    print(f"policy_path={get_policy_path().resolve()}")
    print("observations=" + json.dumps(observations, sort_keys=True))
    print(f"baseline={baseline_score:.2f}")
    print(f"best={best_score:.2f}")
    print(f"delta={best_score - baseline_score:.2f}")
    print(f"min_improvement={float(args.min_improvement):.2f}")
    print(f"trials={result['n_trials']} (complete={result['n_complete']}, pruned={result['n_pruned']})")
    print(f"policy_changed={'yes' if changed else 'no'}")
    print(f"apply_decision={result_record['apply_decision']}")
    
    if importance:
        print("\nparameter_importance:")
        for param, imp in sorted(importance.items(), key=lambda x: -x[1])[:5]:
            print(f"  {param}: {imp:.3f}")
    
    print("\nbest_policy=" + json.dumps(best_policy, sort_keys=True))
    
    if saved_path:
        print(f"applied_policy={saved_path.resolve()}")


if __name__ == "__main__":
    main()
