import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import main

from main import (
    _compute_overlap_watermark,
    _effective_source_diversity,
    _normalize_subagent_task,
    _parse_rescore_statuses,
    _parse_iso_datetime,
    _rescored_trend_candidate_values,
    chunk_records_to_context,
    collect_all_chunks,
    build_source_dedupe_values,
    canonicalize_url,
    chunk_rows_to_records,
    fetch_newsblur,
    fetch_youtube,
    normalize_text_for_hash,
    normalize_trend_text,
    parse_youtube,
    trend_fingerprint,
    upsert_trend_candidate,
)


class FakeCursor:
    def __init__(self, fetchone_results):
        self.fetchone_results = list(fetchone_results)
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))

    def fetchone(self):
        if self.fetchone_results:
            return self.fetchone_results.pop(0)
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, fetchone_results):
        self.fetchone_results = list(fetchone_results)
        self.cursors = []

    def cursor(self):
        cursor = FakeCursor(self.fetchone_results)
        self.cursors.append(cursor)
        return cursor


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeOpener:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def open(self, req, timeout=30):
        self.requests.append((getattr(req, "full_url", ""), timeout))
        return FakeHTTPResponse(self.payload)


class PipelineHelperTests(unittest.TestCase):
    def test_compute_overlap_watermark_subtracts_overlap(self):
        watermark = _compute_overlap_watermark("2026-03-10T12:00:00+00:00", 3600)
        self.assertEqual(watermark.isoformat(), "2026-03-10T11:00:00+00:00")

    def test_canonicalize_url_strips_tracking_params(self):
        url = "https://Example.com/post/?utm_source=x&fbclid=y&id=42#frag"
        self.assertEqual(canonicalize_url(url), "https://example.com/post?id=42")

    def test_build_source_dedupe_values_is_stable_for_whitespace(self):
        first = build_source_dedupe_values(
            {"url": "https://example.com/a?utm_medium=email", "content": "Hello   world"}
        )
        second = build_source_dedupe_values(
            {"url": "https://example.com/a", "content": " hello world "}
        )
        self.assertEqual(first["canonical_url"], "https://example.com/a")
        self.assertEqual(first["url_hash"], second["url_hash"])
        self.assertEqual(first["content_hash"], second["content_hash"])

    def test_chunk_rows_to_records_and_context_are_json_serializable(self):
        rows = [
            (11, 7, "Evidence text", "Source Title", "https://example.com/a", 0.82),
        ]

        records = chunk_rows_to_records(rows)
        self.assertEqual(
            records,
            [
                {
                    "chunk_id": 11,
                    "source_id": 7,
                    "content": "Evidence text",
                    "source_title": "Source Title",
                    "source_url": "https://example.com/a",
                    "score": 0.82,
                }
            ],
        )
        payload = json.loads(chunk_records_to_context(records))
        self.assertEqual(payload[0]["chunk_id"], 11)
        self.assertEqual(payload[0]["source_title"], "Source Title")

    def test_collect_all_chunks_reads_persisted_subagent_artifacts_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            first = base / "first.json"
            second = base / "second.json"
            first.write_text(
                json.dumps(
                    [
                        {"chunk_id": 1, "source_id": 10, "content": "A", "source_title": "T1", "source_url": "u1"},
                        {"chunk_id": 2, "source_id": 11, "content": "B", "source_title": "T2", "source_url": "u2"},
                    ]
                )
            )
            second.write_text(
                json.dumps(
                    [
                        {"chunk_id": 2, "source_id": 11, "content": "B newer", "source_title": "T2", "source_url": "u2"},
                        {"chunk_id": 3, "source_id": 12, "content": "C", "source_title": "T3", "source_url": "u3"},
                    ]
                )
            )

            combined = collect_all_chunks(
                [
                    {"evidence_path": str(first)},
                    {"evidence_path": str(second)},
                ]
            )

        self.assertEqual([record["chunk_id"] for record in combined], [1, 2, 3])
        self.assertEqual(next(record for record in combined if record["chunk_id"] == 2)["content"], "B newer")

    def test_normalize_subagent_task_adds_required_delegation_fields(self):
        task = _normalize_subagent_task(
            {"angle": "Recruitment", "search_queries": ["club recruitment trend"]},
            2,
            "Midfield diamond resurgence",
            "moderate",
        )

        self.assertEqual(task["task_order"], 2)
        self.assertEqual(task["angle"], "Recruitment")
        self.assertTrue(task["objective"].startswith("Find the strongest evidence"))
        self.assertEqual(task["search_queries"], ["club recruitment trend"])
        self.assertIn("markdown brief", task["output_format"])
        self.assertIn("recent", task["search_guidance"])
        self.assertEqual(task["max_rounds"], 3)

    def test_normalize_subagent_task_coerces_structured_text_fields(self):
        task = _normalize_subagent_task(
            {
                "angle": {"name": "Recruitment"},
                "objective": ["find evidence", "avoid fluff"],
                "boundaries": {"scope": "Europe only"},
                "output_format": {"type": "bullet brief"},
                "search_guidance": ["start broad", "then narrow"],
            },
            1,
            "Midfield diamond resurgence",
            "moderate",
        )

        self.assertEqual(task["angle"], '{"name": "Recruitment"}')
        self.assertEqual(task["objective"], '["find evidence", "avoid fluff"]')
        self.assertEqual(task["boundaries"], '{"scope": "Europe only"}')
        self.assertEqual(task["output_format"], '{"type": "bullet brief"}')
        self.assertEqual(task["search_guidance"], '["start broad", "then narrow"]')

    def test_trend_fingerprint_normalizes_punctuation_and_case(self):
        self.assertEqual(normalize_trend_text("High Press in Build-Up!!"), "high press in build up")
        self.assertEqual(
            trend_fingerprint("High Press in Build-Up!!"),
            trend_fingerprint("high press in build up"),
        )

    def test_upsert_trend_candidate_preserves_manual_feedback_and_best_score(self):
        conn = FakeConn(
            [
                (7, "needs_more_evidence", 5, 54, 2),
                (7, 66),
            ]
        )
        candidate = {
            "trend": "Full-back inverts into midfield",
            "reasoning": "Seen across multiple teams.",
            "score": 61,
            "novelty_score": 0.7,
            "source_diversity": 3,
            "sources": [{"source_id": 1}, {"source_id": 2}, {"source_id": 3}],
        }

        candidate_id, final_score, source_diversity = upsert_trend_candidate(conn, candidate, feedback_adjustment=2)

        self.assertEqual(candidate_id, 7)
        self.assertEqual(final_score, 66)
        self.assertEqual(source_diversity, 3)
        executed = conn.cursors[0].executed
        self.assertIn("SELECT id, status, feedback_adjustment, score, source_diversity FROM trend_candidates", executed[0][0])
        self.assertIn("UPDATE trend_candidates SET trend = %s", executed[1][0])
        self.assertEqual(executed[1][1][2], 61)
        self.assertEqual(executed[1][1][3], 5)
        self.assertEqual(executed[1][1][6], 3)
        self.assertEqual(executed[1][1][7], "pending")

    def test_upsert_trend_candidate_inserts_new_candidate(self):
        conn = FakeConn(
            [
                None,
                (11, 48),
            ]
        )
        candidate = {
            "trend": "Winger rotates into half-space",
            "reasoning": "Corroborated early signal.",
            "score": 45,
            "novelty_score": 0.6,
            "source_diversity": 2,
            "sources": [{"source_id": 1}, {"source_id": 2}],
        }

        candidate_id, final_score, source_diversity = upsert_trend_candidate(conn, candidate, feedback_adjustment=0)

        self.assertEqual(candidate_id, 11)
        self.assertEqual(final_score, 48)
        self.assertEqual(source_diversity, 2)
        executed = conn.cursors[0].executed
        self.assertIn("INSERT INTO trend_candidates", executed[1][0])

    def test_effective_source_diversity_prefers_linked_count_when_higher(self):
        self.assertEqual(_effective_source_diversity(2, 5), 5)
        self.assertEqual(_effective_source_diversity(4, 1), 4)

    def test_rescored_trend_candidate_values_recompute_final_score(self):
        source_diversity, final_score = _rescored_trend_candidate_values(
            base_score=60,
            feedback_adjustment=3,
            stored_source_diversity=1,
            linked_source_count=3,
            novelty_score=0.7,
        )

        self.assertEqual(source_diversity, 3)
        self.assertEqual(final_score, 75)

    def test_parse_rescore_statuses_handles_empty_and_csv_values(self):
        self.assertIsNone(_parse_rescore_statuses(""))
        self.assertEqual(
            _parse_rescore_statuses("pending, needs_more_evidence ,reported"),
            ["pending", "needs_more_evidence", "reported"],
        )

    def test_parse_youtube_resolves_noncanonical_sources_without_rewriting_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "youtube.md"
            original_text = "# Channels\n\nExample Channel: https://www.youtube.com/@example\n"
            config_path.write_text(original_text)
            with patch.object(main, "_resolve_uc_channel_id", return_value="UC12345678901234567890"):
                pairs = parse_youtube(config_path)

            self.assertEqual(pairs, [("Example Channel", "UC12345678901234567890")])
            self.assertEqual(config_path.read_text(), original_text)

    def test_fetch_newsblur_extracts_items_even_when_rss_body_is_empty(self):
        opener = FakeOpener(
            {
                "stories": [
                    {
                        "story_title": "",
                        "story_permalink": "https://example.com/post",
                        "story_content": "",
                        "story_summary": "",
                        "id": "story-1",
                        "story_feed_id": 99,
                    }
                ]
            }
        )
        with patch.object(main, "_newsblur_session", return_value=opener), patch.object(
            main,
            "extract_article",
            return_value={
                "content": "Full article body from source page.",
                "author": "Author Name",
                "publish_date": "2026-03-01",
                "sitename": "Example Site",
                "title": "Resolved Title",
                "extraction_method": "trafilatura",
            },
        ):
            items = fetch_newsblur()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Resolved Title")
        self.assertEqual(items[0]["content"], "Full article body from source page.")
        self.assertEqual(items[0]["extraction_method"], "trafilatura")

    def test_fetch_youtube_filters_out_already_seen_videos_by_published_at(self):
        videos = [
            {
                "id": "old-video",
                "title": "Older Video",
                "url": "https://www.youtube.com/watch?v=old-video",
                "published_at": "2026-03-10T00:00:00+00:00",
            },
            {
                "id": "new-video",
                "title": "Newer Video",
                "url": "https://www.youtube.com/watch?v=new-video",
                "published_at": "2026-03-12T00:00:00+00:00",
            },
        ]
        with patch.object(main, "_youtube_rss_latest_videos", return_value=videos), patch.object(
            main, "_transcriptapi_get", return_value={"transcript": "Transcript text"}
        ):
            items, discovery_failed, counters, latest_published_at = fetch_youtube(
                "Example",
                "UC12345678901234567890",
                published_after=_parse_iso_datetime("2026-03-11T00:00:00+00:00"),
            )

        self.assertFalse(discovery_failed)
        self.assertEqual([item["key"] for item in items], ["yt:UC12345678901234567890:new-video"])
        self.assertEqual(counters["youtube_transcript_successes"], 1)
        self.assertEqual(latest_published_at.isoformat(), "2026-03-12T00:00:00+00:00")

    def test_fetch_youtube_http_error_returns_four_tuple_for_ingest_callers(self):
        with patch.object(
            main,
            "_youtube_rss_latest_videos",
            side_effect=HTTPError(
                url="https://www.youtube.com/feeds/videos.xml?channel_id=UC12345678901234567890",
                code=404,
                msg="Not Found",
                hdrs=None,
                fp=None,
            ),
        ):
            items, discovery_failed, counters, latest_published_at = fetch_youtube(
                "Example",
                "UC12345678901234567890",
            )

        self.assertEqual(items, [])
        self.assertTrue(discovery_failed)
        self.assertIsNone(latest_published_at)
        self.assertIn("youtube_discovery_retryable_failures", counters)


if __name__ == "__main__":
    unittest.main()
