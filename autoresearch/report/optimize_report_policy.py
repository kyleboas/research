#!/usr/bin/env python3
"""Bayesian optimization for report policy tuning.

Replaces hand-crafted policy variants with intelligent Bayesian optimization.
Uses no-LLM simulations to project report quality under different resource budgets.
Features:
- Systematic policy space exploration
- Budget-aware optimization
- Warm-start from previous results
- Parameter importance analysis
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

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
from autoresearch.report.evaluator import evaluate_items, load_fixture
from autoresearch.report.export_reports_snapshot import export_snapshot
from autoresearch.report.benchmark_report import (
    DEFAULT_MIN_IMPROVEMENT,
    append_result_row,
    estimate_report_llm_cost,
    policy_changed,
    quality_per_dollar,
    record_report_policy_run,
    report_policy_apply_decision,
)
from db_conn import resolve_database_conninfo
from report_policy import get_policy_path, load_policy, save_policy

DEFAULT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "recent_reports.json"
RESULTS_PATH = Path(__file__).resolve().parent / "results.tsv"
STUDY_STORAGE_PATH = Path(__file__).resolve().parent / ".study_cache.sqlite"


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


# Bayesian search space for report policy
SEARCH_SPACE_BAYESIAN = {
    # Task allocation
    "moderate_min_tasks": ("int_step", 2, 6, 1),
    "complex_min_tasks": ("int_step", 3, 8, 1),
    
    # Research rounds
    "max_research_rounds": ("int_step", 1, 4, 1),
    "simple_default_rounds": ("int_step", 1, 3, 1),
    "moderate_default_rounds": ("int_step", 2, 5, 1),
    "complex_default_rounds": ("int_step", 3, 8, 1),
    
    # Search and token limits
    "subagent_search_limit": ("int_step", 8, 40, 2),
    "subagent_max_tokens": ("int_step", 3000, 8000, 100),
    "synthesis_max_tokens": ("int_step", 6000, 20000, 500),
    "revision_max_tokens": ("int_step", 6000, 20000, 500),
    
    # Cost controls
    "optimize_topic_limit": ("int_step", 1, 3, 1),
    "max_report_llm_cost_usd": ("float_step", 0.5, 1.5, 0.05),
}


def _project_metric(metric: float, capacity_delta: float, positive_weight: float, negative_weight: float) -> float:
    """Project metric value based on capacity changes."""
    current = clamp01(metric)
    if capacity_delta >= 0:
        return clamp01(current + positive_weight * capacity_delta * (1.0 - current))
    return clamp01(current + negative_weight * capacity_delta * current)


def _project_item(item: dict, *, base_policy: dict, candidate_policy: dict) -> dict:
    """Project report quality metrics under candidate policy."""
    # Calculate capacity ratios
    task_ratio = int(candidate_policy["moderate_min_tasks"]) / max(1, int(base_policy["moderate_min_tasks"]))
    round_ratio = int(candidate_policy["max_research_rounds"]) / max(1, int(base_policy["max_research_rounds"]))
    search_ratio = int(candidate_policy["subagent_search_limit"]) / max(1, int(base_policy["subagent_search_limit"]))
    token_ratio = (
        int(candidate_policy["subagent_max_tokens"]) / max(1, int(base_policy["subagent_max_tokens"]))
        + int(candidate_policy["synthesis_max_tokens"]) / max(1, int(base_policy["synthesis_max_tokens"]))
        + int(candidate_policy["revision_max_tokens"]) / max(1, int(base_policy["revision_max_tokens"]))
    ) / 3.0
    
    capacity_delta = (
        0.30 * (task_ratio - 1.0)
        + 0.25 * (round_ratio - 1.0)
        + 0.20 * (search_ratio - 1.0)
        + 0.25 * (token_ratio - 1.0)
    )

    # Project individual metrics
    section_coverage = _project_metric(item.get("section_coverage", 0.0), capacity_delta, 0.18, 0.08)
    citation_validity = _project_metric(item.get("citation_validity", 0.0), capacity_delta, 0.04, 0.02)
    citation_density = _project_metric(item.get("citation_density", 0.0), capacity_delta, 0.12, 0.06)
    source_diversity = _project_metric(item.get("source_diversity_score", 0.0), capacity_delta, 0.10, 0.05)
    sources_section_coverage = _project_metric(item.get("sources_section_coverage", 0.0), capacity_delta, 0.08, 0.04)
    counterevidence_coverage = _project_metric(item.get("counterevidence_coverage", 0.0), capacity_delta, 0.15, 0.08)
    thoroughness = _project_metric(item.get("thoroughness", 0.0), capacity_delta, 0.20, 0.10)

    final_score = round(
        (
            0.22 * section_coverage
            + 0.23 * citation_validity
            + 0.15 * citation_density
            + 0.12 * source_diversity
            + 0.10 * sources_section_coverage
            + 0.08 * counterevidence_coverage
            + 0.10 * thoroughness
        )
        * 100,
        2,
    )

    return {
        "title": item.get("title") or "Untitled report",
        "final_score": final_score,
        "citation_validity": citation_validity,
        "section_coverage": section_coverage,
        "thoroughness": thoroughness,
        "citation_count": item.get("citation_count", 0),
        "invalid_citation_count": item.get("invalid_citation_count", 0),
        "word_count": item.get("word_count", 0),
        "capacity_delta": capacity_delta,
    }


def simulate_policy(items: list[dict], *, base_policy: dict, candidate_policy: dict) -> dict:
    """Simulate report quality under candidate policy (no-LLM)."""
    projected_topics = [_project_item(item, base_policy=base_policy, candidate_policy=candidate_policy) 
                        for item in items]
    average_score = sum(item["final_score"] for item in projected_topics) / len(projected_topics) if projected_topics else 0.0
    estimated_cost_per_report = estimate_report_llm_cost(candidate_policy)
    
    return {
        "policy": dict(candidate_policy),
        "topics": projected_topics,
        "average_score": round(average_score, 2),
        "estimated_cost_per_report": estimated_cost_per_report,
        "quality_per_dollar": quality_per_dollar(average_score, estimated_cost_per_report),
    }


def build_policy_from_params(params: dict, base_policy: dict) -> dict:
    """Build complete policy from sampled parameters."""
    policy = dict(base_policy)
    policy.update(params)
    
    # Enforce hierarchical constraints
    # moderate_min_tasks <= complex_min_tasks
    if policy["moderate_min_tasks"] > policy["complex_min_tasks"]:
        policy["complex_min_tasks"] = policy["moderate_min_tasks"]
    
    # round constraints
    if policy["simple_default_rounds"] > policy["max_research_rounds"]:
        policy["simple_default_rounds"] = policy["max_research_rounds"]
    if policy["moderate_default_rounds"] > policy["max_research_rounds"]:
        policy["moderate_default_rounds"] = policy["max_research_rounds"]
    if policy["complex_default_rounds"] > policy["max_research_rounds"]:
        policy["complex_default_rounds"] = policy["max_research_rounds"]
    
    # Token hierarchy: subagent <= synthesis <= revision (roughly)
    if policy["synthesis_max_tokens"] < policy["subagent_max_tokens"]:
        policy["synthesis_max_tokens"] = policy["subagent_max_tokens"]
    
    return policy


def make_report_objective(items: list, base_policy: dict, budget_limit: float):
    """Create objective function for report policy optimization."""
    
    def objective(trial, params: dict) -> float:
        policy = build_policy_from_params(params, base_policy)
        
        # Simulate quality under this policy
        result = simulate_policy(items, base_policy=base_policy, candidate_policy=policy)
        
        average_score = float(result["average_score"])
        estimated_cost = float(result["estimated_cost_per_report"])
        quality_per_dollar_val = float(result["quality_per_dollar"])
        
        # Budget constraint handling
        if estimated_cost > budget_limit:
            # Heavily penalize over-budget but allow exploration near boundary
            overage = estimated_cost / budget_limit - 1.0
            penalty = 20.0 * overage  # Significant penalty
            return average_score - penalty
        
        # Reward efficiency (quality per dollar)
        # This encourages finding sweet spot of quality vs cost
        efficiency_bonus = quality_per_dollar_val * 0.5
        
        # Small bonus for staying comfortably under budget
        budget_headroom = (budget_limit - estimated_cost) / budget_limit
        budget_bonus = budget_headroom * 0.5
        
        return average_score + efficiency_bonus + budget_bonus
    
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
        # Parse TSV format
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) >= 10:
                try:
                    policy = json.loads(parts[9])  # policy_json column
                    avg_score = float(parts[3])  # average_score column
                    results.append({"params": policy, "value": avg_score})
                except (json.JSONDecodeError, ValueError):
                    continue
        
        return results[-30:] if len(results) > 30 else results
    except Exception:
        return []


def run_legacy_optimizer(args, reason: str):
    from autoresearch.report.optimize_report_policy_legacy import main as legacy_main

    legacy_argv = [sys.argv[0], "--fixture", str(args.fixture), "--limit", str(args.limit)]
    if args.refresh_auto:
        legacy_argv.append("--refresh-auto")
    if args.apply:
        legacy_argv.append("--apply")
    if args.min_improvement != DEFAULT_MIN_IMPROVEMENT:
        legacy_argv.extend(["--min-improvement", str(args.min_improvement)])

    print(f"bayesian_unavailable={reason}")
    print("falling_back_to=legacy_report_optimizer")

    original_argv = sys.argv[:]
    try:
        sys.argv = legacy_argv
        legacy_main()
    finally:
        sys.argv = original_argv


def main():
    parser = argparse.ArgumentParser(
        description="Bayesian optimization for report policy using no-LLM simulations"
    )
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="Path to report fixture JSON")
    parser.add_argument("--limit", type=int, default=2, help="How many recent reports to include")
    parser.add_argument("--refresh-auto", action="store_true", help="Refresh the fixture from Postgres")
    parser.add_argument("--apply", action="store_true", help="Write the best policy to report_policy_config.json")
    parser.add_argument("--min-improvement", type=float, default=DEFAULT_MIN_IMPROVEMENT,
                        help="Minimum average-score improvement required")
    parser.add_argument("--trials", type=int, help="Number of optimization trials")
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

    if args.legacy:
        run_legacy_optimizer(args, "explicit_legacy")
        return

    if not OPTUNA_AVAILABLE:
        run_legacy_optimizer(args, str(OPTUNA_IMPORT_ERROR or "optuna_import_failed"))
        return

    fixture_path = Path(args.fixture)
    if args.refresh_auto:
        fixture_path = export_snapshot(fixture_path, limit=max(1, int(args.limit)))

    items = load_fixture(fixture_path)
    scored = evaluate_items(items)["ranked"]
    scored = scored[:max(1, int(args.limit))]
    topics = [str(item.get("title") or "").strip() for item in scored if str(item.get("title") or "").strip()]
    
    if not scored:
        raise SystemExit("no_reports_available")

    base_policy = load_policy()
    budget_limit = float(base_policy.get("max_report_llm_cost_usd", 1.0) or 1.0)
    
    # Baseline evaluation
    baseline_result = simulate_policy(scored, base_policy=base_policy, candidate_policy=base_policy)
    baseline_score = baseline_result["average_score"]
    baseline_cost = baseline_result["estimated_cost_per_report"]

    # Setup optimization
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
    print(f"Budget limit: ${budget_limit:.2f}")
    print(f"Search space: {len(SEARCH_SPACE_BAYESIAN)} parameters")
    
    try:
        optimizer = BayesianOptimizer(config)
        optimizer.create_study(direction="maximize")
    except ImportError as exc:
        run_legacy_optimizer(args, str(exc))
        return
    
    # Warm-start
    if args.warm_start:
        previous_results = load_previous_results(RESULTS_PATH)
        if previous_results:
            print(f"Warm-starting with {len(previous_results)} previous results...")
            optimizer.warm_start_from_results(previous_results)
    
    # Create objective
    objective = make_report_objective(scored, base_policy, budget_limit)
    
    # Run optimization
    try:
        result = optimizer.optimize(objective, SEARCH_SPACE_BAYESIAN)
    except ImportError as exc:
        run_legacy_optimizer(args, str(exc))
        return
    
    # Build best policy
    best_params = result["best_params"]
    best_policy = build_policy_from_params(best_params, base_policy)
    
    # Evaluate best policy
    best_result = simulate_policy(scored, base_policy=base_policy, candidate_policy=best_policy)
    
    # Budget status
    if float(best_result["estimated_cost_per_report"]) <= budget_limit:
        budget_status = "within_budget"
    else:
        budget_status = "over_budget"
    
    # Apply decision
    changed = policy_changed(base_policy, best_policy)
    applied, apply_decision_value = report_policy_apply_decision(
        baseline_score=baseline_result["average_score"],
        best_score=best_result["average_score"],
        min_improvement=float(args.min_improvement),
        policy_changed=changed,
    )
    
    saved_path = None
    if args.apply and applied:
        saved_path = save_policy(best_policy)

    # Record to database
    conninfo, reason = resolve_database_conninfo()
    if conninfo:
        with psycopg.connect(conninfo) as conn:
            best_result["delta"] = round(best_result["average_score"] - baseline_result["average_score"], 2)
            best_result["applied"] = bool(args.apply and applied)
            best_result["apply_decision"] = apply_decision_value if args.apply else "not_requested"
            best_result["budget_status"] = budget_status
            
            record_report_policy_run(
                conn,
                fixture_path=fixture_path,
                topics=topics,
                baseline_result=baseline_result,
                best_result=best_result,
                min_improvement=float(args.min_improvement),
                applied=bool(args.apply and applied),
                apply_decision=best_result["apply_decision"],
                budget_status=budget_status,
                policy_changed_flag=changed,
            )
            conn.commit()

    # Append results
    append_result_row(fixture_path, topics, best_result)

    # Get parameter importance
    importance = optimizer.get_importance()

    # Print results
    print(f"\n{'='*60}")
    print(f"Optimization Complete")
    print(f"{'='*60}")
    print(f"fixture={fixture_path.resolve()}")
    print(f"policy_path={get_policy_path().resolve()}")
    print(f"topics={json.dumps(topics, ensure_ascii=False)}")
    print(f"baseline={baseline_result['average_score']:.2f} (cost=${baseline_cost:.2f})")
    print(f"best={best_result['average_score']:.2f} (cost=${best_result['estimated_cost_per_report']:.2f})")
    print(f"delta={best_result['average_score'] - baseline_result['average_score']:.2f}")
    print(f"quality_per_dollar={float(best_result['quality_per_dollar']):.4f}")
    print(f"trials={result['n_trials']} (complete={result['n_complete']}, pruned={result['n_pruned']})")
    if result.get("stop_reason"):
        print(f"stop_reason={result['stop_reason']}")
    print(f"budget_status={budget_status}")
    print(f"policy_changed={'yes' if changed else 'no'}")
    print(f"apply_decision={apply_decision_value}")
    
    if importance:
        print("\nparameter_importance:")
        for param, imp in sorted(importance.items(), key=lambda x: -x[1])[:5]:
            print(f"  {param}: {imp:.3f}")
    
    print("\nbest_policy=" + json.dumps(best_policy, sort_keys=True))
    
    if saved_path:
        print(f"applied_policy={saved_path.resolve()}")


if __name__ == "__main__":
    main()
