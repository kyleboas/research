#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autoresearch_report.evaluator import evaluate_items, load_fixture
from autoresearch_report.export_reports_snapshot import export_snapshot
from report_policy import get_policy_path

DEFAULT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "recent_reports.json"


def main():
    parser = argparse.ArgumentParser(description="Evaluate report quality against a recent reports fixture")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="Path to fixture JSON")
    parser.add_argument("--limit", type=int, default=20, help="Export limit when using --refresh-auto")
    parser.add_argument("--refresh-auto", action="store_true", help="Export a fresh fixture from Postgres before evaluation")
    args = parser.parse_args()

    fixture_path = Path(args.fixture)
    if args.refresh_auto:
        fixture_path = export_snapshot(fixture_path, limit=args.limit)

    items = load_fixture(fixture_path)
    result = evaluate_items(items)
    metrics = result["metrics"]

    print(f"fixture={fixture_path.resolve()}")
    print(f"policy={get_policy_path().resolve()}")
    print(f"average_item_score={metrics['average_item_score']:.2f}")
    print(f"section_coverage={metrics['section_coverage']:.4f}")
    print(f"citation_validity={metrics['citation_validity']:.4f}")
    print(f"citation_density={metrics['citation_density']:.4f}")
    print(f"source_diversity={metrics['source_diversity']:.4f}")
    print(f"sources_section_coverage={metrics['sources_section_coverage']:.4f}")
    print(f"counterevidence_coverage={metrics['counterevidence_coverage']:.4f}")
    print(f"thoroughness={metrics['thoroughness']:.4f}")
    print(f"FINAL_SCORE={metrics['final_score']:.2f}")
    print("top_ranked:")
    for item in result["ranked"][:3]:
        print(
            "- "
            + json.dumps(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "final_score": item.get("final_score"),
                    "citations": item.get("citation_count"),
                    "invalid_citations": item.get("invalid_citation_count"),
                    "words": item.get("word_count"),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
