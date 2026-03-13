#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db_conn import resolve_database_conninfo

CITATION_RE = re.compile(r"\[S(\d+):C(\d+)\]")


def _extract_citations(text: str) -> list[tuple[int, int]]:
    return [(int(source_id), int(chunk_id)) for source_id, chunk_id in CITATION_RE.findall(text or "")]


def export_snapshot(output_path: str | Path, limit: int = 20):
    conninfo, reason = resolve_database_conninfo()
    if not conninfo:
        raise SystemExit(f"database_unavailable:{reason}")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, content, metadata::text, created_at
                FROM reports
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = cur.fetchall()

            citation_pairs = set()
            for _report_id, _title, content, _metadata, _created_at in rows:
                citation_pairs.update(_extract_citations(content or ""))

            valid_pairs = set()
            if citation_pairs:
                pair_values = sorted(citation_pairs)
                pair_placeholders = ",".join(["(%s,%s)"] * len(pair_values))
                params = [value for pair in pair_values for value in pair]
                cur.execute(
                    f"""
                    SELECT s.id, c.id
                    FROM chunks c
                    JOIN sources s ON s.id = c.source_id
                    WHERE (s.id, c.id) IN ({pair_placeholders})
                    """,
                    params,
                )
                valid_pairs = {(int(source_id), int(chunk_id)) for source_id, chunk_id in cur.fetchall()}

    payload = []
    for report_id, title, content, metadata_text, created_at in rows:
        citations = _extract_citations(content or "")
        invalid_count = sum(1 for pair in citations if pair not in valid_pairs)
        metadata = {}
        if metadata_text:
            try:
                metadata = json.loads(metadata_text)
            except json.JSONDecodeError:
                metadata = {}
        payload.append(
            {
                "id": int(report_id),
                "title": title or "Untitled report",
                "content": content or "",
                "created_at": created_at.isoformat() if created_at else None,
                "metadata": metadata,
                "citation_validation": {
                    "citation_count": len(citations),
                    "invalid_citation_count": invalid_count,
                },
            }
        )

    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return output


def main():
    parser = argparse.ArgumentParser(description="Export a recent reports snapshot from Postgres")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parent / "fixtures" / "recent_reports.json"),
        help="Path to write the snapshot JSON",
    )
    parser.add_argument("--limit", type=int, default=20, help="Maximum number of recent reports to export")
    args = parser.parse_args()

    output = export_snapshot(args.output, limit=args.limit)
    print(f"fixture={output.resolve()}")
    print(f"reports_exported_limit={int(args.limit)}")


if __name__ == "__main__":
    main()
