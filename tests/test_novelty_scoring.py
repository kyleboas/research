import unittest
from datetime import UTC, datetime, timedelta

from novelty_scoring import compute_novelty_score


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))

    def fetchall(self):
        return list(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, rows):
        self.rows = list(rows)
        self.cursors = []

    def cursor(self):
        cursor = FakeCursor(self.rows)
        self.cursors.append(cursor)
        return cursor


class NoveltyScoringTests(unittest.TestCase):
    def test_returns_high_novelty_when_no_baselines_exist(self):
        score = compute_novelty_score(FakeConn([]), "New concept", [0.1, 0.2], source_count=2)
        self.assertEqual(score, 0.95)

    def test_generic_strategy_language_is_penalized_more_than_specific_tactics(self):
        rows = [
            ("historical concept", 0.22, 1, 1, datetime.now(UTC) - timedelta(days=365)),
        ]
        generic = compute_novelty_score(
            FakeConn(rows),
            "Using data analytics for player recruitment",
            [0.1, 0.2],
            source_count=2,
        )
        tactical = compute_novelty_score(
            FakeConn(rows),
            "Full-back inverts into midfield during build-up",
            [0.1, 0.2],
            source_count=2,
        )
        self.assertLess(generic, tactical)

    def test_recent_similar_baselines_reduce_novelty(self):
        recent_rows = [
            ("recent concept", 0.76, 2, 2, datetime.now(UTC) - timedelta(days=2)),
        ]
        stale_rows = [
            ("stale concept", 0.76, 2, 2, datetime.now(UTC) - timedelta(days=240)),
        ]

        recent = compute_novelty_score(
            FakeConn(recent_rows),
            "Winger rotates into the half-space",
            [0.2, 0.4],
            source_count=3,
        )
        stale = compute_novelty_score(
            FakeConn(stale_rows),
            "Winger rotates into the half-space",
            [0.2, 0.4],
            source_count=3,
        )
        self.assertLess(recent, stale)

    def test_crowded_prevalent_neighborhood_reduces_novelty(self):
        crowded_rows = [
            ("idea a", 0.68, 18, 10, datetime.now(UTC) - timedelta(days=120)),
            ("idea b", 0.66, 14, 8, datetime.now(UTC) - timedelta(days=160)),
            ("idea c", 0.64, 11, 6, datetime.now(UTC) - timedelta(days=200)),
        ]
        sparse_rows = [
            ("idea a", 0.68, 1, 1, datetime.now(UTC) - timedelta(days=200)),
        ]

        crowded = compute_novelty_score(
            FakeConn(crowded_rows),
            "Keeper-driven central progression from goal kicks",
            [0.3, 0.7],
            source_count=3,
        )
        sparse = compute_novelty_score(
            FakeConn(sparse_rows),
            "Keeper-driven central progression from goal kicks",
            [0.3, 0.7],
            source_count=3,
        )
        self.assertLess(crowded, sparse)


if __name__ == "__main__":
    unittest.main()
