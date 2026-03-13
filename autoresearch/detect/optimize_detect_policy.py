#!/usr/bin/env python3
"""Bayesian optimization for detect policy tuning.

Replaces the exhaustive grid search with intelligent Bayesian optimization using Optuna.
Features:
- Early stopping for unpromising configurations
- Warm-start from previous results
- Budget-aware optimization
- Parameter importance analysis
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, UTC
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autoresearch.bayesian_optimizer import (
    BayesianOptimizer,
    OPTIMIZATION_PRESETS,
    OPTUNA_AVAILABLE,
    OPTUNA_IMPORT_ERROR,
    clone_optimization_config,
)
from autoresearch.detect.evaluator import evaluate_items, load_fixture
from autoresearch.detect.export_candidates_snapshot import export_snapshot
from detect_policy import DEFAULT_POLICY, get_policy_path, load_policy, save_policy

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
AUTO_FIXTURE_PATH = FIXTURES_DIR / "live_candidates.auto.json"
RESULTS_PATH = Path(__file__).resolve().parent / "results.tsv"
STUDY_STORAGE_PATH = Path(__file__).resolve().parent / ".study_cache.sqlite"

# Bayesian search space (continuous ranges where possible)
# Using int_step to allow fine-grained exploration
SEARCH_SPACE_BAYESIAN = {
    "novelty_weight": ("int_step", 15, 50, 1),
    "single_source_penalty": ("int_step", -25, -2, 1),
    "few_sources_bonus": ("int_step", 0, 12, 1),
    "several_sources_bonus": ("int_step", -5, 6, 1),
    "many_sources_penalty": ("int_step", -12, 0, 1),
    "report_min_score": ("int_step", 30, 60, 1),
    "report_min_sources": ("int_step", 1, 5, 1),
}

# Budget constraints - no LLM re-runs, just policy evaluation
EVALUATION_BUDGET_CENTS = 0.01  # Minimal compute cost per eval


def default_fixture_path():
    for path in (
        FIXTURES_DIR / "live_candidates.labeled.json",
        FIXTURES_DIR / "live_candidates.json",
        FIXTURES_DIR / "candidates.json",
    ):
        if path.exists():
            return path
    return FIXTURES_DIR / "candidates.json"


def policy_distance(policy):
    """Calculate how far a policy deviates from defaults (for regularization)."""
    distance = 0
    for key, default_value in DEFAULT_POLICY.items():
        value = policy.get(key, default_value)
        distance += abs(float(value) - float(default_value))
    return distance


def build_policy_from_params(params: dict, base_policy: dict) -> dict:
    """Build a complete policy from sampled parameters."""
    policy = dict(base_policy)
    policy.update(params)
    
    # Enforce constraint: report_min_sources must be <= few_sources_max
    # This is a hard constraint that must always hold
    if policy["report_min_sources"] > policy["few_sources_max"]:
        # Adjust to satisfy constraint
        policy["report_min_sources"] = policy["few_sources_max"]
    
    return policy


def make_detect_objective(items: list, top_k: int, baseline_metrics: dict):
    """Create objective function for detect policy optimization."""
    baseline_score = baseline_metrics["final_score"]
    
    def objective(trial, params: dict) -> float:
        policy = build_policy_from_params(params, DEFAULT_POLICY)
        
        result = evaluate_items(items, policy=policy, top_k=top_k)
        metrics = result["metrics"]
        
        # Primary objective: final_score
        score = float(metrics["final_score"])
        
        # Secondary objectives as tiebreakers
        gate_acc = float(metrics["gate_accuracy"])
        recall = float(metrics["report_recall"])
        
        # Regularization: prefer policies closer to defaults (less deviation)
        distance_penalty = policy_distance(policy) * 0.01
        
        # Composite score with all components
        # Weight final_score heavily, use others for discrimination
        composite = (
            score * 1.0 +
            gate_acc * 5.0 +
            recall * 3.0 -
            distance_penalty
        )
        
        return composite
    
    return objective


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
        # Skip header
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) >= 8:
                try:
                    policy = json.loads(parts[7])
                    final_score = float(parts[2])
                    results.append({"params": policy, "value": final_score})
                except (json.JSONDecodeError, ValueError):
                    continue
        
        # Return most recent results (last 50)
        return results[-50:] if len(results) > 50 else results
    except Exception:
        return []


def append_result_row(fixture_path: Path, metrics: dict, policy: dict, optimization_info: dict | None = None):
    """Append optimization result to results.tsv."""
    header = "timestamp\tfixture\tfinal_score\tprecision_at_k\tpairwise_accuracy\tgat_accuracy\treport_recall\tpolicy_json\toptimization_type\tn_trials\tbest_trial\n"
    
    if not RESULTS_PATH.exists() or not RESULTS_PATH.read_text().startswith(header):
        RESULTS_PATH.write_text(header)
    
    opt_type = optimization_info.get("type", "bayesian") if optimization_info else "bayesian"
    n_trials = str(optimization_info.get("n_trials", "")) if optimization_info else ""
    best_trial = str(optimization_info.get("best_trial", "")) if optimization_info else ""
    
    row = "\t".join(
        [
            datetime.now(UTC).isoformat(),
            str(fixture_path),
            f"{metrics['final_score']:.2f}",
            f"{metrics['precision_at_k']:.4f}",
            f"{metrics['pairwise_accuracy']:.4f}",
            f"{metrics['gate_accuracy']:.4f}",
            f"{metrics['report_recall']:.4f}",
            json.dumps(policy, sort_keys=True),
            opt_type,
            n_trials,
            best_trial,
        ]
    )
    with RESULTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(row + "\n")


def ensure_auto_fixture(limit: int):
    """Export fresh fixture from database."""
    export_snapshot(AUTO_FIXTURE_PATH, limit=limit, label_mode="auto")
    return AUTO_FIXTURE_PATH


def run_legacy_optimizer(args, reason: str):
    from autoresearch.detect.optimize_detect_policy_legacy import main as legacy_main

    legacy_argv = [sys.argv[0], "--top-k", str(args.top_k), "--limit", str(args.limit)]
    if args.fixture:
        legacy_argv.extend(["--fixture", str(args.fixture)])
    if args.refresh_auto:
        legacy_argv.append("--refresh-auto")
    if args.apply:
        legacy_argv.append("--apply")

    print(f"bayesian_unavailable={reason}")
    print("falling_back_to=legacy_detect_optimizer")

    original_argv = sys.argv[:]
    try:
        sys.argv = legacy_argv
        legacy_main()
    finally:
        sys.argv = original_argv


def main():
    parser = argparse.ArgumentParser(
        description="Bayesian optimization for detect policy settings using Optuna"
    )
    parser.add_argument("--fixture", help="Path to labeled fixture JSON")
    parser.add_argument("--top-k", type=int, default=3, help="Precision cutoff")
    parser.add_argument("--limit", type=int, default=100, help="Candidate export limit when using --refresh-auto")
    parser.add_argument("--refresh-auto", action="store_true", help="Export a fresh auto-labeled fixture")
    parser.add_argument("--apply", action="store_true", help="Write the best policy to detect_policy_config.json")
    parser.add_argument("--trials", type=int, help="Number of optimization trials")
    parser.add_argument("--preset", choices=["fast", "thorough", "budget_constrained", "exploration"],
                        default="fast", help="Optimization preset")
    parser.add_argument("--timeout", type=float, help="Maximum optimization time in seconds")
    parser.add_argument("--warm-start", action="store_true", default=True,
                        help="Warm-start from previous results")
    parser.add_argument("--no-warm-start", dest="warm_start", action="store_false",
                        help="Disable warm-start")
    parser.add_argument("--study-name", help="Name for the Optuna study")
    parser.add_argument("--legacy", action="store_true", help="Use legacy grid search instead")
    args = parser.parse_args()

    if args.legacy:
        run_legacy_optimizer(args, "explicit_legacy")
        return

    if not OPTUNA_AVAILABLE:
        run_legacy_optimizer(args, str(OPTUNA_IMPORT_ERROR or "optuna_import_failed"))
        return

    # Load fixture
    if args.refresh_auto:
        fixture_path = ensure_auto_fixture(limit=args.limit)
    else:
        fixture_path = Path(args.fixture) if args.fixture else default_fixture_path()

    items = load_fixture(fixture_path)
    baseline_policy = load_policy()
    baseline = evaluate_items(items, policy=baseline_policy, top_k=args.top_k)
    baseline_metrics = baseline["metrics"]

    if baseline_metrics["labeled_positive"] == 0 or baseline_metrics["labeled_negative"] == 0:
        raise SystemExit(
            "not_enough_labels: need at least one positive and one negative labeled candidate"
        )

    # Setup optimization configuration
    config = clone_optimization_config(OPTIMIZATION_PRESETS[args.preset])
    if args.trials is not None:
        config.n_trials = args.trials
    if args.timeout:
        config.timeout_seconds = args.timeout
    if args.study_name:
        config.study_name = args.study_name
    config.storage_path = str(STUDY_STORAGE_PATH)

    print(f"Starting Bayesian optimization with {config.n_trials} trials...")
    print(f"Preset: {args.preset}")
    print(f"Search space: {len(SEARCH_SPACE_BAYESIAN)} parameters")
    
    try:
        optimizer = BayesianOptimizer(config)
        optimizer.create_study(direction="maximize")
    except ImportError as exc:
        run_legacy_optimizer(args, str(exc))
        return
    
    # Warm-start from previous results if available
    if args.warm_start:
        previous_results = load_previous_results(RESULTS_PATH)
        if previous_results:
            print(f"Warm-starting with {len(previous_results)} previous results...")
            optimizer.warm_start_from_results(previous_results)
    
    # Create objective function
    objective = make_detect_objective(items, args.top_k, baseline_metrics)
    
    # Run optimization
    try:
        result = optimizer.optimize(objective, SEARCH_SPACE_BAYESIAN)
    except ImportError as exc:
        run_legacy_optimizer(args, str(exc))
        return
    
    # Build best policy from optimized parameters
    best_params = result["best_params"]
    best_policy = build_policy_from_params(best_params, baseline_policy)
    
    # Evaluate best policy to get full metrics
    best_result = evaluate_items(items, policy=best_policy, top_k=args.top_k)
    best_metrics = best_result["metrics"]
    
    # Get parameter importance
    importance = optimizer.get_importance()
    
    # Log results
    append_result_row(
        Path(fixture_path),
        best_metrics,
        best_policy,
        {
            "type": "bayesian",
            "n_trials": result["n_trials"],
            "best_trial": result.get("best_trial", "unknown"),
            "n_pruned": result.get("n_pruned", 0),
        }
    )

    # Print results
    print(f"\n{'='*60}")
    print(f"Optimization Complete")
    print(f"{'='*60}")
    print(f"fixture={Path(fixture_path).resolve()}")
    print(f"policy_path={get_policy_path().resolve()}")
    print(
        f"baseline={baseline_metrics['final_score']:.2f} "
        f"(labels={baseline_metrics['labeled_items']}, "
        f"positives={baseline_metrics['labeled_positive']}, negatives={baseline_metrics['labeled_negative']})"
    )
    print(f"best={best_metrics['final_score']:.2f}")
    print(f"delta={best_metrics['final_score'] - baseline_metrics['final_score']:.2f}")
    print(f"trials={result['n_trials']} (complete={result['n_complete']}, pruned={result['n_pruned']})")
    if result.get("stop_reason"):
        print(f"stop_reason={result['stop_reason']}")
    
    if importance:
        print("\nparameter_importance:")
        for param, imp in sorted(importance.items(), key=lambda x: -x[1])[:5]:
            print(f"  {param}: {imp:.3f}")
    
    print("\nbest_policy=" + json.dumps(best_policy, sort_keys=True))
    print("\ntop_ranked:")
    for item in best_result["ranked"][:args.top_k]:
        print(
            f"- rank={item['rank']} id={item['id']} final_score={item['final_score']} "
            f"expected={item.get('expected')} gate={item['passes_gate']} trend={item['trend']}"
        )

    if args.apply:
        saved_path = save_policy(best_policy)
        print(f"\napplied_policy={saved_path.resolve()}")


if __name__ == "__main__":
    main()
