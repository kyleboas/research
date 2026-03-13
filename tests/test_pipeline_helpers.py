import unittest

from main import (
    _effective_source_diversity,
    _parse_rescore_statuses,
    _rescored_trend_candidate_values,
    build_source_dedupe_values,
    canonicalize_url,
    normalize_text_for_hash,
    normalize_trend_text,
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


class PipelineHelperTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
