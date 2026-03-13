#!/usr/bin/env python3
"""Legacy hand-crafted policy optimization for report tuning.

Original implementation using 4 manually designed policy variants.
Kept for backward compatibility and comparison purposes.
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


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def candidate_policies(base_policy: dict):
    """Generate 4 hand-crafted policy variants."""
    candidates = [
        dict(base_policy),
        {
            **base_policy,
            "moderate_min_tasks": max(2, int(base_policy["moderate_min_tasks"])),
            "complex_min_tasks": max(3, int(base_policy["complex_min_tasks"]) - 1),
            "subagent_search_limit": max(12, int(base_policy["subagent_search_limit"]) - 4),
            "subagent_max_tokens": max(4000, int(base_policy["subagent_max_tokens"]) - 500),
            "synthesis_max_tokens": max(9000, int(base_policy["synthesis_max_tokens"]) - 1000),
            "revision_max_tokens": max(9000, int(base_policy["revision_max_tokens"]) - 1000),
            "optimize_topic_limit": min(int(base_policy.get("optimize_topic_limit", 2) or 2), 2),
            "max_report_llm_cost_usd": min(float(base_policy.get("max_report_llm_cost_usd", 0.85) or 0.85), 0.75),
        },
        {
            **base_policy,
            "moderate_min_tasks": max(3, int(base_policy["moderate_min_tasks"])),
            "complex_min_tasks": max(5, int(base_policy["complex_min_tasks"])),
            "subagent_search_limit": max(24, int(base_policy["subagent_search_limit"])),
            "subagent_max_tokens": max(6000, int(base_policy["subagent_max_tokens"])),
            "synthesis_max_tokens": max(14000, int(base_policy["synthesis_max_tokens"])),
            "revision_max_tokens": max(14000, int(base_policy["revision_max_tokens"])),
            "optimize_topic_limit": min(int(base_policy.get("optimize_topic_limit", 2) or 2), 2),
            "max_report_llm_cost_usd": max(float(base_policy.get("max_report_llm_cost_usd", 0.85) or 0.85), 0.85),
        },
        {
            **base_policy,
            "moderate_min_tasks": min(5, int(base_policy["moderate_min_tasks"]) + 1),
            "complex_min_tasks": min(7, int(base_policy["complex_min_tasks"]) + 1),
            "subagent_search_limit": min(36, int(base_policy["subagent_search_limit"]) + 6),
            "subagent_max_tokens": min(8000, int(base_policy["subagent_max_tokens"]) + 2000),
            "synthesis_max_tokens": min(18000, int(base_policy["synthesis_max_tokens"]) + 4000),
            "revision_max_tokens": min(18000, int(base_policy["revision_max_tokens"]) + 4000),
            "optimize_topic_limit": min(2, int(base_policy.get("optimize_topic_limit", 2) or 2)),
            "max_report_llm_cost_usd": max(float(base_policy.get("max_report_llm_cost_usd", 0.85) or 0.85), 1.0),
        },
    ]
    seen = set()
    for policy in candidates:
        key = json.dumps(policy, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        yield policy


def _project_metric(metric: float, capacity_delta: float, positive_weight: float, negative_weight: float) -> float:
    current = clamp01(metric)
    if capacity_delta >= 0:
        return clamp01(current + positive_weight * capacity_delta * (1.0 - current))
    return clamp01(current + negative_weight * capacity_delta * current)


def _project_item(item: dict, *, base_policy: dict, candidate_policy: dict) -> dict:
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
    }


def simulate_policy(items: list[dict], *, base_policy: dict, candidate_policy: dict) -> dict:
    projected_topics = [_project_item(item, base_policy=base_policy, candidate_policy=candidate_policy) for item in items]
    average_score = sum(item["final_score"] for item in projected_topics) / len(projected_topics) if projected_topics else 0.0
    estimated_cost_per_report = estimate_report_llm_cost(candidate_policy)
    return {
        "policy": dict(candidate_policy),
        "topics": projected_topics,
        "average_score": round(average_score, 2),
        "estimated_cost_per_report": estimated_cost_per_report,
        "quality_per_dollar": quality_per_dollar(average_score, estimated_cost_per_report),
    }


def main():
    parser = argparse.ArgumentParser(description="[LEGACY] Optimize report policy with hand-crafted variants")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="Path to report fixture JSON")
    parser.add_argument("--limit", type=int, default=2, help="How many recent reports to include")
    parser.add_argument("--refresh-auto", action="store_true", help="Refresh fixture from Postgres")
    parser.add_argument("--apply", action="store_true", help="Write the best policy")
    parser.add_argument("--min-improvement", type=float, default=DEFAULT_MIN_IMPROVEMENT, help="Min improvement required")
    args = parser.parse_args()

    fixture_path = Path(args.fixture)
    if args.refresh_auto:
        fixture_path = export_snapshot(fixture_path, limit=max(1, int(args.limit)))

    items = load_fixture(fixture_path)
    scored = evaluate_items(items)["ranked"]
    scored = scored[: max(1, int(args.limit))]
    topics = [str(item.get("title") or "").strip() for item in scored if str(item.get("title") or "").strip()]
    if not scored:
        raise SystemExit("no_reports_available")

    base_policy = load_policy()
    baseline_result = simulate_policy(scored, base_policy=base_policy, candidate_policy=base_policy)
    best_result = baseline_result
    budget_limit = float(base_policy.get("max_report_llm_cost_usd", 1.0) or 1.0)
    within_budget = []
    over_budget = []

    for policy in candidate_policies(base_policy):
        result = simulate_policy(scored, base_policy=base_policy, candidate_policy=policy)
        if float(result["estimated_cost_per_report"]) <= budget_limit:
            within_budget.append(result)
        else:
            over_budget.append(result)

    if within_budget:
        best_result = max(within_budget, key=lambda result: (float(result["average_score"]), float(result["quality_per_dollar"])))
        budget_status = "within_budget"
    else:
        best_result = max([baseline_result, *over_budget], key=lambda result: (float(result["quality_per_dollar"]), float(result["average_score"])))
        budget_status = "no_candidate_within_budget"

    changed = policy_changed(base_policy, best_result["policy"])
    applied, apply_decision_value = report_policy_apply_decision(
        baseline_score=baseline_result["average_score"],
        best_score=best_result["average_score"],
        min_improvement=float(args.min_improvement),
        policy_changed=changed,
    )
    if args.apply and applied:
        saved_path = save_policy(best_result["policy"])
    else:
        saved_path = None

    conninfo, reason = resolve_database_conninfo()
    if not conninfo:
        raise SystemExit(f"database_unavailable:{reason}")
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

    append_result_row(fixture_path, topics, best_result)

    print(f"fixture={fixture_path.resolve()}")
    print(f"policy_path={get_policy_path().resolve()}")
    print(f"topics={json.dumps(topics, ensure_ascii=False)}")
    print(f"baseline={baseline_result['average_score']:.2f}")
    print(f"best={best_result['average_score']:.2f}")
    print(f"delta={best_result['average_score'] - baseline_result['average_score']:.2f}")
    print(f"estimated_cost_per_report={float(best_result['estimated_cost_per_report']):.2f}")
    print(f"quality_per_dollar={float(best_result['quality_per_dollar']):.4f}")
    print(f"max_report_llm_cost_usd={budget_limit:.2f}")
    print(f"budget_status={budget_status}")
    print(f"min_improvement={float(args.min_improvement):.2f}")
    print(f"policy_changed={'yes' if changed else 'no'}")
    print(f"apply_decision={best_result['apply_decision']}")
    print("best_policy=" + json.dumps(best_result["policy"], sort_keys=True))
    if saved_path is not None:
        print(f"applied_policy={saved_path.resolve()}")


if __name__ == "__main__":
    main()
