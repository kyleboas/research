#!/usr/bin/env python3
"""Legacy exhaustive grid search for detect policy tuning.

This is the original brute-force grid search implementation.
Kept for backward compatibility and comparison purposes.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime, UTC
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autoresearch.detect.evaluator import evaluate_items, load_fixture
from autoresearch.detect.export_candidates_snapshot import export_snapshot
from detect_policy import DEFAULT_POLICY, get_policy_path, load_policy, save_policy

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
AUTO_FIXTURE_PATH = FIXTURES_DIR / "live_candidates.auto.json"
RESULTS_PATH = Path(__file__).resolve().parent / "results.tsv"

SEARCH_SPACE = {
    "novelty_weight": [20, 25, 30, 35, 40],
    "single_source_penalty": [-20, -16, -12, -8, -4],
    "few_sources_bonus": [0, 3, 6, 9],
    "several_sources_bonus": [-2, 0, 2, 4],
    "many_sources_penalty": [-10, -8, -6, -4, 0],
    "report_min_score": [35, 40, 45, 50],
    "report_min_sources": [2, 3],
}


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
    distance = 0
    for key, default_value in DEFAULT_POLICY.items():
        value = policy.get(key, default_value)
        distance += abs(float(value) - float(default_value))
    return distance


def candidate_policies(base_policy: dict):
    search_keys = list(SEARCH_SPACE)
    search_values = [SEARCH_SPACE[key] for key in search_keys]
    for combo in itertools.product(*search_values):
        policy = dict(base_policy)
        policy.update(dict(zip(search_keys, combo, strict=True)))
        if policy["report_min_sources"] > policy["few_sources_max"]:
            continue
        yield policy


def ensure_auto_fixture(limit: int):
    export_snapshot(AUTO_FIXTURE_PATH, limit=limit, label_mode="auto")
    return AUTO_FIXTURE_PATH


def append_result_row(fixture_path: Path, metrics: dict, policy: dict):
    header = "timestamp\tfixture\tfinal_score\tprecision_at_k\tpairwise_accuracy\tgat_accuracy\treport_recall\tpolicy_json\n"
    if not RESULTS_PATH.exists() or not RESULTS_PATH.read_text().startswith(header):
        RESULTS_PATH.write_text(header)
    row = "\t".join(
        [
            datetime.now(UTC).isoformat(),
            str(fixture_path),
            f"{metrics['final_score']:.2f}",
            f"{metrics['precision_at_k']:.4f}",
            f"{metrics['pairwise_accuracy']:.4f}",
            f"{metrics['gat_accuracy']:.4f}",
            f"{metrics['report_recall']:.4f}",
            json.dumps(policy, sort_keys=True),
        ]
    )
    with RESULTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(row + "\n")


def main():
    parser = argparse.ArgumentParser(description="[LEGACY] Exhaustive grid search for detect policy")
    parser.add_argument("--fixture", help="Path to labeled fixture JSON")
    parser.add_argument("--top-k", type=int, default=3, help="Precision cutoff")
    parser.add_argument("--limit", type=int, default=100, help="Candidate export limit")
    parser.add_argument("--refresh-auto", action="store_true", help="Export a fresh fixture")
    parser.add_argument("--apply", action="store_true", help="Write the best policy")
    args = parser.parse_args()

    if args.refresh_auto:
        fixture_path = ensure_auto_fixture(limit=args.limit)
    else:
        fixture_path = Path(args.fixture) if args.fixture else default_fixture_path()

    items = load_fixture(fixture_path)
    baseline_policy = load_policy()
    baseline = evaluate_items(items, policy=baseline_policy, top_k=args.top_k)
    baseline_metrics = baseline["metrics"]

    if baseline_metrics["labeled_positive"] == 0 or baseline_metrics["labeled_negative"] == 0:
        raise SystemExit("not_enough_labels: need at least one positive and one negative labeled candidate")

    best_policy = dict(baseline_policy)
    best_result = baseline

    total_combinations = 1
    for values in SEARCH_SPACE.values():
        total_combinations *= len(values)
    
    print(f"Running exhaustive grid search over {total_combinations} combinations...")
    evaluated = 0

    for policy in candidate_policies(baseline_policy):
        result = evaluate_items(items, policy=policy, top_k=args.top_k)
        metrics = result["metrics"]
        best_metrics = best_result["metrics"]
        if (
            metrics["final_score"],
            metrics["gat_accuracy"],
            metrics["report_recall"],
            -policy_distance(policy),
        ) > (
            best_metrics["final_score"],
            best_metrics["gat_accuracy"],
            best_metrics["report_recall"],
            -policy_distance(best_policy),
        ):
            best_policy = policy
            best_result = result
        evaluated += 1
        if evaluated % 1000 == 0:
            print(f"  Evaluated {evaluated}/{total_combinations}...")

    best_metrics = best_result["metrics"]
    append_result_row(Path(fixture_path), best_metrics, best_policy)

    print(f"fixture={Path(fixture_path).resolve()}")
    print(f"policy_path={get_policy_path().resolve()}")
    print(f"baseline={baseline_metrics['final_score']:.2f}")
    print(f"best={best_metrics['final_score']:.2f}")
    print(f"delta={best_metrics['final_score'] - baseline_metrics['final_score']:.2f}")
    print(f"evaluated={evaluated} combinations")
    print("best_policy=" + json.dumps(best_policy, sort_keys=True))

    if args.apply:
        saved_path = save_policy(best_policy)
        print(f"applied_policy={saved_path.resolve()}")


if __name__ == "__main__":
    main()
