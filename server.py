import json
import os
from datetime import UTC, datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

import psycopg

from db_conn import resolve_database_conninfo

PORT = int(os.environ.get("PORT", 8080))


class DashboardHandler(SimpleHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fetch_dashboard_payload(self):
        conninfo, reason = resolve_database_conninfo()
        if not conninfo:
            warning = f"database_unavailable:{reason}"
            return {
                "logs": [],
                "ingest": [],
                "detect": [],
                "reports": [],
                "status": [
                    {"label": "Workflow live", "value": "Unknown"},
                    {"label": "Database", "value": "Unavailable"},
                ],
                "warning": warning,
            }

        with psycopg.connect(conninfo) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT title, url, source_type, LEFT(content, 255), created_at
                    FROM sources
                    ORDER BY created_at DESC, id DESC
                    """
                )
                ingest_items = [
                    {
                        "title": row[0] or "Untitled",
                        "url": row[1] or "",
                        "source_type": row[2] or "unknown",
                        "description": row[3] or "",
                        "created_at": row[4].isoformat() if row[4] else None,
                    }
                    for row in cur.fetchall()
                ]

                cur.execute(
                    """
                    SELECT trend, reasoning, score, status, detected_at
                    FROM trend_candidates
                    ORDER BY score DESC, detected_at DESC, id DESC
                    LIMIT 50
                    """
                )
                detect_items = [
                    {
                        "trend": row[0],
                        "reasoning": row[1] or "",
                        "score": row[2],
                        "status": row[3],
                        "detected_at": row[4].isoformat() if row[4] else None,
                    }
                    for row in cur.fetchall()
                ]

                cur.execute(
                    """
                    SELECT title, content, metadata, created_at
                    FROM reports
                    ORDER BY created_at DESC, id DESC
                    LIMIT 100
                    """
                )
                report_items = []
                for row in cur.fetchall():
                    metadata = row[2] if isinstance(row[2], dict) else {}
                    report_items.append(
                        {
                            "title": row[0] or "Untitled report",
                            "description": (row[1] or "")[:255],
                            "url": (metadata.get("url") or "") if isinstance(metadata, dict) else "",
                            "created_at": row[3].isoformat() if row[3] else None,
                        }
                    )

                cur.execute("SELECT COUNT(*) FROM sources")
                sources_count = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM trend_candidates")
                trends_count = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM reports")
                reports_count = cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT key, value
                    FROM pipeline_state
                    WHERE key IN ('last_ingest_completed_at', 'last_ingest_new_sources')
                    """
                )
                state = {row[0]: row[1] for row in cur.fetchall()}

                cur.execute(
                    """
                    SELECT event, detail, event_time
                    FROM (
                        SELECT COALESCE(NULLIF(title, ''), 'Ingested source') AS event,
                               COALESCE(NULLIF(source_type, ''), url, '') AS detail,
                               created_at AS event_time
                        FROM sources
                        UNION ALL
                        SELECT 'Trend candidate detected' AS event,
                               trend AS detail,
                               detected_at AS event_time
                        FROM trend_candidates
                        UNION ALL
                        SELECT 'Report generated' AS event,
                               COALESCE(title, 'Untitled report') AS detail,
                               created_at AS event_time
                        FROM reports
                    ) AS combined
                    ORDER BY event_time DESC
                    LIMIT 60
                    """
                )
                logs = [
                    {
                        "event": row[0],
                        "detail": row[1] or "",
                        "time": row[2].isoformat() if row[2] else None,
                    }
                    for row in cur.fetchall()
                ]

        now = datetime.now(UTC)
        status_items = [
            {"label": "Workflow live", "value": "Yes"},
            {"label": "Database", "value": "Connected"},
            {"label": "Total ingested sources", "value": str(sources_count)},
            {"label": "Total trend candidates", "value": str(trends_count)},
            {"label": "Total reports", "value": str(reports_count)},
            {"label": "Last ingest completed", "value": state.get("last_ingest_completed_at", "Unknown")},
            {"label": "Sources added in last ingest", "value": state.get("last_ingest_new_sources", "Unknown")},
            {"label": "Status generated at", "value": now.isoformat()},
        ]

        return {
            "logs": logs,
            "ingest": ingest_items,
            "detect": detect_items,
            "reports": report_items,
            "status": status_items,
            "warning": None,
        }

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/dashboard":
            try:
                payload = self._fetch_dashboard_payload()
            except Exception as exc:
                self._send_json(
                    {
                        "logs": [],
                        "ingest": [],
                        "detect": [],
                        "reports": [],
                        "status": [],
                        "warning": f"failed_to_fetch_dashboard:{exc}",
                    },
                    status=500,
                )
                return

            self._send_json(payload)
            return

        if self.path == "/":
            self.path = "/dashboard.html"
        return super().do_GET()


if __name__ == "__main__":
    httpd = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"Serving dashboard at http://0.0.0.0:{PORT}/")
    httpd.serve_forever()
