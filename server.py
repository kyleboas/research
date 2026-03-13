import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from html import escape
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import psycopg
from dotenv import load_dotenv

from db_conn import resolve_database_conninfo
from detect_policy import compute_final_score

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
PORT = int(os.environ.get("PORT", 8080))

RUN_COMMANDS = {
    "ingest": [sys.executable, str(ROOT / "main.py"), "--step", "ingest"],
    "detect": [sys.executable, str(ROOT / "main.py"), "--step", "detect"],
    "rescore": [sys.executable, str(ROOT / "main.py"), "--step", "rescore"],
    "report": [sys.executable, str(ROOT / "main.py"), "--step", "report"],
    "detect_policy_eval": [sys.executable, str(ROOT / "autoresearch_detect" / "eval_detect.py")],
    "detect_policy_optimize": [
        sys.executable,
        str(ROOT / "autoresearch_detect" / "optimize_detect_policy.py"),
        "--refresh-auto",
        "--apply",
    ],
}
_run_lock = Lock()
_step_runs = {
    "ingest": {"status": "idle", "started_at": None, "finished_at": None, "exit_code": None, "log_tail": ""},
    "detect": {"status": "idle", "started_at": None, "finished_at": None, "exit_code": None, "log_tail": ""},
    "rescore": {"status": "idle", "started_at": None, "finished_at": None, "exit_code": None, "log_tail": ""},
    "report": {"status": "idle", "started_at": None, "finished_at": None, "exit_code": None, "log_tail": ""},
    "detect_policy_eval": {"status": "idle", "started_at": None, "finished_at": None, "exit_code": None, "log_tail": ""},
    "detect_policy_optimize": {"status": "idle", "started_at": None, "finished_at": None, "exit_code": None, "log_tail": ""},
}
_active_processes = {}


def _read_log_tail(log_path, max_chars=2000):
    if not log_path:
        return ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= 0:
                return ""
            f.seek(max(0, size - 8192), os.SEEK_SET)
            tail = f.read() or ""
    except Exception:
        return ""

    tail = tail.strip()
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def _utc_now_iso():
    return datetime.now(UTC).isoformat()


def _refresh_step_runs():
    with _run_lock:
        for step, run_meta in list(_active_processes.items()):
            proc = run_meta["proc"]
            log_path = run_meta.get("log_path")
            if proc.poll() is None:
                _step_runs[step]["log_tail"] = _read_log_tail(log_path)
                continue
            _active_processes.pop(step, None)
            state = _step_runs[step]
            state["status"] = "success" if proc.returncode == 0 else "failed"
            state["finished_at"] = _utc_now_iso()
            state["exit_code"] = proc.returncode

            log_file = run_meta.get("log_file")
            try:
                if log_file:
                    log_file.close()
            except Exception:
                pass
            state["log_tail"] = _read_log_tail(log_path)


def _step_runs_snapshot():
    with _run_lock:
        return {k: dict(v) for k, v in _step_runs.items()}


def _start_step_run(step):
    _refresh_step_runs()
    with _run_lock:
        if step not in RUN_COMMANDS:
            return False, "invalid_step"
        if any(meta["proc"].poll() is None for meta in _active_processes.values()):
            return False, "another_step_running"

        log_file = tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", delete=False)
        cmd = RUN_COMMANDS[step]
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=os.environ.copy(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _active_processes[step] = {"proc": proc, "log_file": log_file, "log_path": log_file.name}
        _step_runs[step] = {
            "status": "running",
            "started_at": _utc_now_iso(),
            "finished_at": None,
            "exit_code": None,
            "log_tail": "",
        }
        return True, None


class DashboardHandler(SimpleHTTPRequestHandler):
    def _report_record(self, row, *, include_content=False):
        metadata = row[2] if isinstance(row[2], dict) else {}
        report_id = int(row[0])
        record = {
            "id": report_id,
            "title": row[1] or "Untitled report",
            "description": (row[3] or "")[:255],
            "view_url": f"/reports/{report_id}",
            "external_url": (metadata.get("url") or "") if isinstance(metadata, dict) else "",
            "created_at": row[4].isoformat() if row[4] else None,
            "metadata": metadata,
        }
        if include_content:
            record["content"] = row[3] or ""
        return record

    def _fetch_report_by_id(self, report_id):
        conninfo, reason = resolve_database_conninfo()
        if not conninfo:
            raise RuntimeError(f"database_unavailable:{reason}")

        with psycopg.connect(conninfo) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, title, metadata, content, created_at
                    FROM reports
                    WHERE id = %s
                    LIMIT 1
                    """,
                    (report_id,),
                )
                row = cur.fetchone()
                return self._report_record(row, include_content=True) if row else None

    def _send_html(self, body, status=200):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _render_report_page(self, report_id):
        try:
            record = self._fetch_report_by_id(report_id)
        except Exception as exc:
            self._send_html(
                f"<html><body><h1>Unable to load report</h1><p>{escape(str(exc))}</p></body></html>",
                status=500,
            )
            return

        if not record:
            self._send_html(
                "<html><body><h1>Report not found</h1><p>The requested report does not exist.</p></body></html>",
                status=404,
            )
            return

        meta = record.get("metadata") or {}
        meta_parts = []
        if meta.get("complexity"):
            meta_parts.append(f"complexity: {meta['complexity']}")
        if meta.get("research_rounds") is not None:
            meta_parts.append(f"rounds: {meta['research_rounds']}")
        if meta.get("total_chunks") is not None:
            meta_parts.append(f"chunks: {meta['total_chunks']}")
        if meta.get("angles"):
            meta_parts.append(f"angles: {len(meta['angles'])}")
        meta_line = " · ".join(meta_parts)
        external_url = record.get("external_url") or ""

        body = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{escape(record["title"])}</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f6f4ee;
        --paper: #fffdf7;
        --line: #ddd6c8;
        --text: #1d1b18;
        --subtext: #6d655a;
        --accent: #0f766e;
      }}
      body {{
        margin: 0;
        background: linear-gradient(180deg, #efe9db 0%, var(--bg) 28%);
        color: var(--text);
        font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
      }}
      main {{
        max-width: 920px;
        margin: 0 auto;
        padding: 32px 18px 56px;
      }}
      .card {{
        background: var(--paper);
        border: 1px solid var(--line);
        border-radius: 18px;
        padding: 24px;
        box-shadow: 0 18px 45px rgba(66, 51, 28, 0.08);
      }}
      h1 {{
        margin: 0 0 10px;
        line-height: 1.05;
        font-size: clamp(2rem, 4vw, 3rem);
      }}
      .meta {{
        color: var(--subtext);
        margin: 0 0 18px;
        font-size: 0.98rem;
      }}
      .actions {{
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        margin: 0 0 22px;
      }}
      .btn {{
        display: inline-block;
        border-radius: 999px;
        border: 1px solid var(--line);
        color: var(--text);
        text-decoration: none;
        padding: 8px 14px;
        font-weight: 600;
        background: #fff;
      }}
      .btn-primary {{
        background: var(--accent);
        border-color: var(--accent);
        color: #fff;
      }}
      pre {{
        margin: 0;
        white-space: pre-wrap;
        word-break: break-word;
        line-height: 1.65;
        font-size: 1rem;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
        background: #fcfaf4;
        border: 1px solid var(--line);
        border-radius: 14px;
        padding: 18px;
        overflow: auto;
      }}
      @media (max-width: 640px) {{
        main {{ padding: 18px 12px 34px; }}
        .card {{ padding: 18px; border-radius: 14px; }}
      }}
    </style>
  </head>
  <body>
    <main>
      <article class="card">
        <h1>{escape(record["title"])}</h1>
        <p class="meta">{escape(record.get("created_at") or "—")}{' · ' + escape(meta_line) if meta_line else ''}</p>
        <div class="actions">
          <a class="btn" href="/dashboard.html#reports">Back to dashboard</a>
          <a class="btn btn-primary" href="/api/reports/{record['id']}/raw">Open raw markdown</a>
          {f'<a class="btn" href="{escape(external_url)}" target="_blank" rel="noreferrer">External link</a>' if external_url else ''}
        </div>
        <pre>{escape(record.get("content") or "")}</pre>
      </article>
    </main>
  </body>
</html>"""
        self._send_html(body)

    def _ensure_sources_metadata_columns(self, cur):
        """Backfill newer ingestion columns for older `sources` tables."""
        cur.execute(
            """
            ALTER TABLE sources
                ADD COLUMN IF NOT EXISTS author TEXT,
                ADD COLUMN IF NOT EXISTS publish_date DATE,
                ADD COLUMN IF NOT EXISTS sitename TEXT,
                ADD COLUMN IF NOT EXISTS extraction_method TEXT DEFAULT 'rss'
            """
        )

    def _resolve_feedback_storage_value(self, cur, delta):
        """Use a feedback value compatible with existing DB constraints.

        Some environments may have an older CHECK constraint that allows only
        {-1, 1}. Newer schemas permit {-5, -1, 1, 5}. This method keeps writes
        compatible with either shape.
        """
        cur.execute(
            """
            SELECT pg_get_constraintdef(c.oid)
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE t.relname = 'trend_feedback'
              AND c.contype = 'c'
              AND pg_get_constraintdef(c.oid) LIKE '%feedback_value%'
            """
        )
        for (constraint_def,) in cur.fetchall():
            if "-5" in constraint_def and "5" in constraint_def:
                return delta

        return 1 if delta > 0 else -1

    def _ensure_trend_candidate_scoring_columns(self, cur):
        cur.execute(
            """
            ALTER TABLE trend_candidates
                ADD COLUMN IF NOT EXISTS trend_fingerprint TEXT,
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
        _refresh_step_runs()
        conninfo, reason = resolve_database_conninfo()
        if not conninfo:
            warning = f"database_unavailable:{reason}"
            return {
                "logs": [],
                "ingest": [],
                "detect": [],
                "reports": [],
                "patterns": [],
                "status": [],
                "step_runs": _step_runs_snapshot(),
                "warning": warning,
            }

        with psycopg.connect(conninfo) as conn:
            with conn.cursor() as cur:
                self._ensure_sources_metadata_columns(cur)
                self._ensure_trend_candidate_scoring_columns(cur)

                # ── Ingest: include extraction metadata ──
                cur.execute(
                    """
                    SELECT title, url, source_type, LEFT(content, 255),
                           created_at, author, sitename, extraction_method,
                           publish_date, LENGTH(content) AS content_length
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
                        "author": row[5] or None,
                        "sitename": row[6] or None,
                        "extraction_method": row[7] or "rss",
                        "publish_date": row[8].isoformat() if row[8] else None,
                        "content_length": row[9] or 0,
                    }
                    for row in cur.fetchall()
                ]

                cur.execute("SELECT to_regclass('trend_candidate_sources')")
                has_trend_candidate_sources = cur.fetchone()[0] is not None

                # ── Detect: include novelty_score, source_diversity ──
                # Check if new columns exist
                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'trend_candidates'
                    AND column_name IN ('novelty_score', 'source_diversity')
                    """
                )
                available_cols = {row[0] for row in cur.fetchall()}
                has_novelty = "novelty_score" in available_cols
                has_diversity = "source_diversity" in available_cols

                novelty_select = "tc.novelty_score" if has_novelty else "NULL AS novelty_score"
                diversity_select = "tc.source_diversity" if has_diversity else "0 AS source_diversity"

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
                        ),
                        {novelty},
                        {diversity}
                    FROM trend_candidates tc
                    {source_join}
                    GROUP BY tc.id
                    ORDER BY COALESCE(tc.final_score, tc.score) DESC, tc.detected_at DESC, tc.id DESC
                    LIMIT 50
                """.format(
                    novelty=novelty_select,
                    diversity=diversity_select,
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
                        "novelty_score": round(float(row[9]), 4) if row[9] is not None else None,
                        "source_diversity": row[10] or 0,
                    }
                    for row in cur.fetchall()
                ]

                # ── Reports ──
                cur.execute(
                    """
                    SELECT id, title, metadata, content, created_at
                    FROM reports
                    ORDER BY created_at DESC, id DESC
                    LIMIT 100
                    """
                )
                report_items = [self._report_record(row) for row in cur.fetchall()]

                # ── Tactical patterns ──
                cur.execute("SELECT to_regclass('tactical_patterns')")
                has_patterns = cur.fetchone()[0] is not None

                pattern_items = []
                if has_patterns:
                    cur.execute(
                        """
                        SELECT tp.id, tp.actor, tp.action, LEFT(tp.context, 200),
                               tp.zones, tp.phase, tp.created_at,
                               s.title AS source_title, s.url AS source_url
                        FROM tactical_patterns tp
                        LEFT JOIN sources s ON s.id = tp.source_id
                        ORDER BY tp.created_at DESC
                        LIMIT 100
                        """
                    )
                    for row in cur.fetchall():
                        pattern_items.append({
                            "id": row[0],
                            "actor": row[1] or "",
                            "action": row[2] or "",
                            "context": row[3] or "",
                            "zones": row[4] if isinstance(row[4], list) else [],
                            "phase": row[5] or "",
                            "created_at": row[6].isoformat() if row[6] else None,
                            "source_title": row[7] or "Unknown",
                            "source_url": row[8] or "",
                        })

                # ── Counts ──
                cur.execute("SELECT COUNT(*) FROM sources")
                sources_count = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM chunks")
                chunks_count = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM trend_candidates")
                trends_count = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM reports")
                reports_count = cur.fetchone()[0]

                patterns_count = 0
                if has_patterns:
                    cur.execute("SELECT COUNT(*) FROM tactical_patterns")
                    patterns_count = cur.fetchone()[0]

                baselines_count = 0
                cur.execute("SELECT to_regclass('novelty_baselines')")
                if cur.fetchone()[0] is not None:
                    cur.execute("SELECT COUNT(*) FROM novelty_baselines")
                    baselines_count = cur.fetchone()[0]

                # Extraction method breakdown
                cur.execute(
                    """
                    SELECT COALESCE(extraction_method, 'rss') AS method, COUNT(*)
                    FROM sources
                    GROUP BY method ORDER BY COUNT(*) DESC
                    """
                )
                extraction_breakdown = {row[0]: row[1] for row in cur.fetchall()}

                # Source type breakdown
                cur.execute(
                    """
                    SELECT source_type, COUNT(*)
                    FROM sources
                    GROUP BY source_type ORDER BY COUNT(*) DESC
                    """
                )
                source_type_breakdown = {row[0]: row[1] for row in cur.fetchall()}

                # Avg content length by extraction method
                cur.execute(
                    """
                    SELECT COALESCE(extraction_method, 'rss') AS method,
                           ROUND(AVG(LENGTH(content))) AS avg_len
                    FROM sources
                    GROUP BY method ORDER BY avg_len DESC
                    """
                )
                avg_content_length = {row[0]: int(row[1]) for row in cur.fetchall()}

                # Pipeline state
                cur.execute(
                    """
                    SELECT key, value
                    FROM pipeline_state
                    """
                )
                state = {row[0]: row[1] for row in cur.fetchall()}

                # ── Logs ──
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
            {"label": "Total sources", "value": str(sources_count)},
            {"label": "Total chunks", "value": str(chunks_count)},
            {"label": "Tactical patterns", "value": str(patterns_count)},
            {"label": "Novelty baselines", "value": str(baselines_count)},
            {"label": "Trend candidates", "value": str(trends_count)},
            {"label": "Reports", "value": str(reports_count)},
            {"label": "Last ingest", "value": state.get("last_ingest_completed_at", "Unknown")},
            {"label": "New sources (last ingest)", "value": state.get("last_ingest_new_sources", "Unknown")},
            {"label": "Generated at", "value": now.isoformat()},
        ]

        return {
            "logs": logs,
            "ingest": ingest_items,
            "detect": detect_items,
            "reports": report_items,
            "patterns": pattern_items,
            "status": status_items,
            "step_runs": _step_runs_snapshot(),
            "debug": {
                "extraction_breakdown": extraction_breakdown,
                "source_type_breakdown": source_type_breakdown,
                "avg_content_length": avg_content_length,
                "pipeline_state": state,
                "counts": {
                    "sources": sources_count,
                    "chunks": chunks_count,
                    "tactical_patterns": patterns_count,
                    "novelty_baselines": baselines_count,
                    "trend_candidates": trends_count,
                    "reports": reports_count,
                },
            },
            "warning": None,
        }

    def _trigger_pipeline_step(self):
        payload = self._read_json_body()
        step = str(payload.get("step") or "").strip().lower()
        ok, err = _start_step_run(step)
        if not ok:
            status_code = 400 if err == "invalid_step" else 409
            self._send_json({"ok": False, "error": err, "step_runs": _step_runs_snapshot()}, status=status_code)
            return

        self._send_json({"ok": True, "step": step, "step_runs": _step_runs_snapshot()}, status=202)

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
                feedback_value = self._resolve_feedback_storage_value(cur, delta)
                cur.execute(
                    "INSERT INTO trend_feedback (trend_candidate_id, trend_text, feedback_value, note) VALUES (%s, %s, %s, %s)",
                    (trend_candidate_id, trend_text, feedback_value, note or None),
                )
                cur.execute(
                    """
                    UPDATE trend_candidates
                    SET feedback_adjustment = feedback_adjustment + %s,
                        status = CASE WHEN status = 'reported' THEN 'reported' ELSE 'pending' END
                    WHERE id = %s
                    RETURNING score, feedback_adjustment, novelty_score, COALESCE(source_diversity, 0), status
                    """,
                    (delta, trend_candidate_id),
                )
                updated = cur.fetchone()
                final_score = compute_final_score(
                    base_score=updated[0],
                    novelty_score=updated[2],
                    feedback_adjustment=updated[1],
                    source_diversity=updated[3],
                )
                cur.execute(
                    "UPDATE trend_candidates SET final_score = %s WHERE id = %s",
                    (final_score, trend_candidate_id),
                )
            conn.commit()

        self._send_json(
            {
                "ok": True,
                "trend_candidate_id": trend_candidate_id,
                "base_score": updated[0],
                "feedback_adjustment": updated[1],
                "score": final_score,
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

        if parsed.path == "/api/run-step":
            try:
                self._trigger_pipeline_step()
            except Exception as exc:
                self._send_json({"ok": False, "error": f"failed_to_trigger_step:{exc}"}, status=500)
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

        if parsed.path.startswith("/api/reports/") and parsed.path.endswith("/raw"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "reports" and parts[3] == "raw":
                try:
                    report_id = int(parts[2])
                except ValueError:
                    self._send_json({"ok": False, "error": "invalid_report_id"}, status=400)
                    return
                try:
                    record = self._fetch_report_by_id(report_id)
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=500)
                    return
                if not record:
                    self._send_json({"ok": False, "error": "report_not_found"}, status=404)
                    return

                body = (record.get("content") or "").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        if parsed.path.startswith("/reports/"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 2 and parts[0] == "reports":
                try:
                    report_id = int(parts[1])
                except ValueError:
                    self._send_html("<html><body><h1>Invalid report id</h1></body></html>", status=400)
                    return
                self._render_report_page(report_id)
                return

        if self.path == "/":
            self.path = "/dashboard.html"
        return super().do_GET()


if __name__ == "__main__":
    httpd = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"Serving dashboard at http://0.0.0.0:{PORT}/")
    httpd.serve_forever()
