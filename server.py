import json
import os
from datetime import UTC, datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

import psycopg

from db_conn import resolve_database_conninfo

PORT = int(os.environ.get("PORT", 8080))


class DashboardHandler(SimpleHTTPRequestHandler):
    def _ensure_trend_candidate_scoring_columns(self, cur):
        cur.execute(
            """
            ALTER TABLE trend_candidates
                ADD COLUMN IF NOT EXISTS feedback_adjustment INT NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS final_score INT
            """
        )

    def _ensure_trend_feedback_table(self, cur):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS trend_feedback (
                id BIGSERIAL PRIMARY KEY,
                trend_candidate_id BIGINT NOT NULL REFERENCES trend_candidates(id) ON DELETE CASCADE,
                trend_text TEXT NOT NULL,
                feedback_value INT NOT NULL CHECK (feedback_value IN (-5, -1, 1, 5)),
                note TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trend_feedback_created_at
            ON trend_feedback (created_at DESC)
            """
        )

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

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
                self._ensure_trend_candidate_scoring_columns(cur)

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

                cur.execute("SELECT to_regclass('trend_candidate_sources')")
                has_trend_candidate_sources = cur.fetchone()[0] is not None

                detect_query = """
                    SELECT
                        tc.id,
                        tc.trend,
                        tc.reasoning,
                        tc.score,
                        tc.feedback_adjustment,
                        COALESCE(tc.final_score, tc.score) AS display_score,
                        tc.status,
                        tc.detected_at,
                        COALESCE(
                            json_agg(
                                json_build_object(
                                    'id', s.id,
                                    'title', COALESCE(s.title, 'Untitled source'),
                                    'url', COALESCE(s.url, '')
                                )
                                ORDER BY s.created_at DESC, s.id DESC
                            ) FILTER (WHERE s.id IS NOT NULL),
                            '[]'::json
                        )
                    FROM trend_candidates tc
                    {source_join}
                    GROUP BY tc.id
                    ORDER BY COALESCE(tc.final_score, tc.score) DESC, tc.detected_at DESC, tc.id DESC
                    LIMIT 50
                """.format(
                    source_join=(
                        """
                        LEFT JOIN trend_candidate_sources tcs ON tcs.trend_candidate_id = tc.id
                        LEFT JOIN sources s ON s.id = tcs.source_id
                        """
                        if has_trend_candidate_sources
                        else "LEFT JOIN sources s ON FALSE"
                    )
                )
                cur.execute(detect_query)
                detect_items = [
                    {
                        "id": row[0],
                        "trend": row[1],
                        "reasoning": row[2] or "",
                        "score": row[5],
                        "base_score": row[3],
                        "feedback_adjustment": row[4] or 0,
                        "status": row[6],
                        "detected_at": row[7].isoformat() if row[7] else None,
                        "sources": row[8] if isinstance(row[8], list) else [],
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

    def _record_trend_feedback(self):
        payload = self._read_json_body()
        trend_candidate_id = payload.get("trend_candidate_id")
        feedback_kind = str(payload.get("feedback") or "").strip().lower()
        note = str(payload.get("note") or "").strip()

        if feedback_kind not in {"important", "not_important"}:
            self._send_json({"ok": False, "error": "invalid_feedback"}, status=400)
            return

        try:
            trend_candidate_id = int(trend_candidate_id)
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "invalid_trend_candidate_id"}, status=400)
            return

        delta = 5 if feedback_kind == "important" else -5

        conninfo, reason = resolve_database_conninfo()
        if not conninfo:
            self._send_json({"ok": False, "error": f"database_unavailable:{reason}"}, status=503)
            return

        with psycopg.connect(conninfo) as conn:
            with conn.cursor() as cur:
                self._ensure_trend_candidate_scoring_columns(cur)
                self._ensure_trend_feedback_table(cur)

                cur.execute("SELECT trend FROM trend_candidates WHERE id = %s", (trend_candidate_id,))
                row = cur.fetchone()
                if not row:
                    self._send_json({"ok": False, "error": "trend_not_found"}, status=404)
                    return

                trend_text = row[0] or ""
                cur.execute(
                    "INSERT INTO trend_feedback (trend_candidate_id, trend_text, feedback_value, note) VALUES (%s, %s, %s, %s)",
                    (trend_candidate_id, trend_text, delta, note or None),
                )
                cur.execute(
                    """
                    UPDATE trend_candidates
                    SET feedback_adjustment = feedback_adjustment + %s,
                        final_score = GREATEST(0, LEAST(100, score + feedback_adjustment + %s))
                    WHERE id = %s
                    RETURNING score, feedback_adjustment, COALESCE(final_score, score)
                    """,
                    (delta, delta, trend_candidate_id),
                )
                updated = cur.fetchone()
            conn.commit()

        self._send_json(
            {
                "ok": True,
                "trend_candidate_id": trend_candidate_id,
                "base_score": updated[0],
                "feedback_adjustment": updated[1],
                "score": updated[2],
            }
        )

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/trend-feedback":
            try:
                self._record_trend_feedback()
            except Exception as exc:
                self._send_json({"ok": False, "error": f"failed_to_record_feedback:{exc}"}, status=500)
            return

        self._send_json({"ok": False, "error": "not_found"}, status=404)

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
