#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autoresearch_report.evaluator import score_report
from autoresearch_report.eval_report import DEFAULT_FIXTURE
from autoresearch_report.export_reports_snapshot import export_snapshot
from db_conn import resolve_database_conninfo
from main import generate_report, set_report_policy
from report_policy import get_policy_path, load_policy, save_policy

RESULTS_PATH = Path(__file__).resolve().parent / "benchmark_results.tsv"
DEFAULT_MIN_IMPROVEMENT = 1.0

def load_fixture(path: str | Path):
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, list):
        raise ValueError("fixture must be a JSON array")
    return payload


def candidate_policies(base_policy: dict):
    candidates = [
        {
            **base_policy,
            "moderate_min_tasks": max(int(base_policy["moderate_min_tasks"]), 4),
            "complex_min_tasks": max(int(base_policy["complex_min_tasks"]), 6),
            "subagent_search_limit": max(int(base_policy["subagent_search_limit"]), 30),
        },
        {
            **base_policy,
            "synthesis_max_tokens": max(int(base_policy["synthesis_max_tokens"]), 20000),
            "revision_max_tokens": max(int(base_policy["revision_max_tokens"]), 20000),
        },
        {
            **base_policy,
            "max_research_rounds": max(int(base_policy["max_research_rounds"]), 3),
        },
        {
            **base_policy,
            "max_research_rounds": max(int(base_policy["max_research_rounds"]), 3),
            "moderate_min_tasks": max(int(base_policy["moderate_min_tasks"]), 4),
            "complex_min_tasks": max(int(base_policy["complex_min_tasks"]), 6),
            "subagent_search_limit": max(int(base_policy["subagent_search_limit"]), 30),
            "synthesis_max_tokens": max(int(base_policy["synthesis_max_tokens"]), 20000),
            "revision_max_tokens": max(int(base_policy["revision_max_tokens"]), 20000),
        },
    ]
    seen = {json.dumps(base_policy, sort_keys=True)}
    for policy in candidates:
        key = json.dumps(policy, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        yield policy


def policy_changed(base_policy: dict, candidate_policy: dict) -> bool:
    return json.dumps(base_policy, sort_keys=True) != json.dumps(candidate_policy, sort_keys=True)


def report_policy_apply_decision(
    *,
    baseline_score: float,
    best_score: float,
    min_improvement: float,
    policy_changed: bool,
) -> tuple[bool, str]:
    if not policy_changed:
        return False, "no_policy_change"
    if float(best_score) - float(baseline_score) < float(min_improvement):
        return False, "below_min_improvement"
    return True, "applied"


def _extract_citations(text: str):
    import re

    return [(int(a), int(b)) for a, b in re.findall(r"\[S(\d+):C(\d+)\]", text or "")]


def _validate_citations(conn, content: str) -> dict:
    citations = _extract_citations(content)
    if not citations:
        return {"citation_count": 0, "invalid_citation_count": 0}
    unique_pairs = sorted(set(citations))
    placeholders = ",".join(["(%s,%s)"] * len(unique_pairs))
    params = [value for pair in unique_pairs for value in pair]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.id, c.id
            FROM chunks c
            JOIN sources s ON s.id = c.source_id
            WHERE (s.id, c.id) IN ({placeholders})
            """,
            params,
        )
        valid_pairs = {(int(source_id), int(chunk_id)) for source_id, chunk_id in cur.fetchall()}
    invalid_count = sum(1 for pair in citations if pair not in valid_pairs)
    return {"citation_count": len(citations), "invalid_citation_count": invalid_count}


def benchmark_policy(conn, topics: list[str], policy: dict) -> dict:
    set_report_policy(policy)
    topic_results = []
    for topic in topics:
        report = generate_report(
            conn,
            topic,
            persist_report=False,
            publish_to_github=False,
            write_local_post=False,
        )
        scored = score_report(
            {
                "title": topic,
                "content": report,
                "citation_validation": _validate_citations(conn, report),
            }
        )
        topic_results.append(
            {
                "title": topic,
                "final_score": scored["final_score"],
                "citation_validity": scored["citation_validity"],
                "section_coverage": scored["section_coverage"],
                "thoroughness": scored["thoroughness"],
                "citation_count": scored["citation_count"],
                "invalid_citation_count": scored["invalid_citation_count"],
                "word_count": scored["word_count"],
            }
        )
    average_score = sum(item["final_score"] for item in topic_results) / len(topic_results) if topic_results else 0.0
    return {
        "policy": dict(policy),
        "topics": topic_results,
        "average_score": round(average_score, 2),
    }


def append_result_row(fixture_path: Path, topics: list[str], result: dict):
    header = "timestamp\tfixture\ttopic_count\taverage_score\tdelta\tapplied\tapply_decision\tpolicy_json\n"
    if not RESULTS_PATH.exists() or not RESULTS_PATH.read_text().startswith(header):
        RESULTS_PATH.write_text(header)
    row = "\t".join(
        [
            datetime.now(UTC).isoformat(),
            str(fixture_path),
            str(len(topics)),
            f"{float(result['average_score']):.2f}",
            f"{float(result.get('delta', 0.0)):.2f}",
            "yes" if result.get("applied") else "no",
            str(result.get("apply_decision") or ""),
            json.dumps(result["policy"], sort_keys=True),
        ]
    )
    with RESULTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(row + "\n")


def ensure_report_policy_runs_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS report_policy_runs (
                id BIGSERIAL PRIMARY KEY,
                fixture_path TEXT NOT NULL,
                topic_count INT NOT NULL DEFAULT 0,
                topics JSONB NOT NULL DEFAULT '[]'::jsonb,
                baseline_score DOUBLE PRECISION NOT NULL,
                best_score DOUBLE PRECISION NOT NULL,
                delta DOUBLE PRECISION NOT NULL,
                min_improvement DOUBLE PRECISION NOT NULL DEFAULT 0,
                applied BOOLEAN NOT NULL DEFAULT FALSE,
                apply_decision TEXT NOT NULL DEFAULT '',
                policy_changed BOOLEAN NOT NULL DEFAULT FALSE,
                baseline_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
                best_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
                topic_scores JSONB NOT NULL DEFAULT '[]'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_report_policy_runs_created_at
            ON report_policy_runs (created_at DESC)
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


def record_report_policy_run(
    conn,
    *,
    fixture_path: Path,
    topics: list[str],
    baseline_result: dict,
    best_result: dict,
    min_improvement: float,
    applied: bool,
    apply_decision: str,
    policy_changed_flag: bool,
):
    ensure_report_policy_runs_table(conn)
    delta = float(best_result["average_score"]) - float(baseline_result["average_score"])
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO report_policy_runs (
                fixture_path,
                topic_count,
                topics,
                baseline_score,
                best_score,
                delta,
                min_improvement,
                applied,
                apply_decision,
                policy_changed,
                baseline_policy,
                best_policy,
                topic_scores
            ) VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
            """,
            (
                str(fixture_path),
                len(topics),
                json.dumps(topics, ensure_ascii=False),
                float(baseline_result["average_score"]),
                float(best_result["average_score"]),
                delta,
                float(min_improvement),
                bool(applied),
                apply_decision,
                bool(policy_changed_flag),
                json.dumps(baseline_result["policy"], sort_keys=True),
                json.dumps(best_result["policy"], sort_keys=True),
                json.dumps(best_result.get("topics") or [], ensure_ascii=False),
            ),
        )
    save_pipeline_state(conn, "last_report_policy_run_at", datetime.now(UTC).isoformat())
    save_pipeline_state(conn, "last_report_policy_baseline", f"{float(baseline_result['average_score']):.2f}")
    save_pipeline_state(conn, "last_report_policy_best", f"{float(best_result['average_score']):.2f}")
    save_pipeline_state(conn, "last_report_policy_delta", f"{delta:.2f}")
    save_pipeline_state(conn, "last_report_policy_applied", "yes" if applied else "no")
    save_pipeline_state(conn, "last_report_policy_apply_decision", apply_decision)


def main():
    parser = argparse.ArgumentParser(description="Benchmark report generation under candidate report policies")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="Path to report fixture JSON")
    parser.add_argument("--limit", type=int, default=3, help="How many recent report titles to benchmark")
    parser.add_argument("--refresh-auto", action="store_true", help="Refresh the fixture from Postgres before benchmarking")
    parser.add_argument("--apply", action="store_true", help="Write the best policy to report_policy_config.json")
    parser.add_argument(
        "--min-improvement",
        type=float,
        default=DEFAULT_MIN_IMPROVEMENT,
        help="Minimum average-score improvement required before applying a new report policy",
    )
    args = parser.parse_args()

    fixture_path = Path(args.fixture)
    if args.refresh_auto:
        fixture_path = export_snapshot(fixture_path, limit=args.limit)

    fixture = load_fixture(fixture_path)
    topics = [str(item.get("title") or "").strip() for item in fixture if str(item.get("title") or "").strip()]
    topics = topics[: max(1, int(args.limit))]
    if not topics:
        raise SystemExit("no_topics_available")

    conninfo, reason = resolve_database_conninfo()
    if not conninfo:
        raise SystemExit(f"database_unavailable:{reason}")

    base_policy = load_policy()
    best_result = None
    baseline_result = None
    applied = False
    apply_decision = "not_requested"
    policy_changed_flag = False

    with psycopg.connect(conninfo) as conn:
        baseline_result = benchmark_policy(conn, topics, base_policy)
        best_result = baseline_result
        for policy in candidate_policies(base_policy):
            result = benchmark_policy(conn, topics, policy)
            if result["average_score"] > best_result["average_score"]:
                best_result = result
        policy_changed_flag = policy_changed(base_policy, best_result["policy"])
        if args.apply:
            applied, apply_decision = report_policy_apply_decision(
                baseline_score=baseline_result["average_score"],
                best_score=best_result["average_score"],
                min_improvement=float(args.min_improvement),
                policy_changed=policy_changed_flag,
            )
            if applied:
                saved_path = save_policy(best_result["policy"])
            else:
                saved_path = None
        record_report_policy_run(
            conn,
            fixture_path=fixture_path,
            topics=topics,
            baseline_result=baseline_result,
            best_result=best_result,
            min_improvement=float(args.min_improvement),
            applied=applied,
            apply_decision=apply_decision,
            policy_changed_flag=policy_changed_flag,
        )
        conn.commit()

    assert best_result is not None
    assert baseline_result is not None
    best_result["delta"] = round(best_result["average_score"] - baseline_result["average_score"], 2)
    best_result["applied"] = applied
    best_result["apply_decision"] = apply_decision
    append_result_row(fixture_path, topics, best_result)

    print(f"fixture={fixture_path.resolve()}")
    print(f"policy_path={get_policy_path().resolve()}")
    print(f"topics={json.dumps(topics, ensure_ascii=False)}")
    print(f"baseline={baseline_result['average_score']:.2f}")
    print(f"best={best_result['average_score']:.2f}")
    print(f"delta={best_result['average_score'] - baseline_result['average_score']:.2f}")
    print(f"min_improvement={float(args.min_improvement):.2f}")
    print(f"policy_changed={'yes' if policy_changed_flag else 'no'}")
    print(f"apply_decision={apply_decision}")
    print("best_policy=" + json.dumps(best_result["policy"], sort_keys=True))
    print("topic_scores:")
    for item in best_result["topics"]:
        print(
            "- "
            + json.dumps(
                {
                    "title": item["title"],
                    "score": item["final_score"],
                    "words": item["word_count"],
                    "citations": item["citation_count"],
                    "invalid_citations": item["invalid_citation_count"],
                },
                ensure_ascii=False,
            )
        )

    if args.apply and applied:
        print(f"applied_policy={saved_path.resolve()}")


if __name__ == "__main__":
    main()
