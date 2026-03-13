import unittest

from autoresearch.ingest.optimize_ingest_policy import ensure_ingest_policy_runs_table


class IngestPolicyOptimizerTests(unittest.TestCase):
    def test_ensure_ingest_policy_runs_table_backfills_bayesian_columns(self):
        executed = []

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                executed.append((sql, params))

        class FakeConn:
            def cursor(self):
                return FakeCursor()

        ensure_ingest_policy_runs_table(FakeConn())

        normalized_sql = [" ".join(sql.split()) for sql, _params in executed]
        self.assertTrue(
            any(
                "ALTER TABLE ingest_policy_runs ADD COLUMN IF NOT EXISTS optimization_type TEXT NOT NULL DEFAULT 'bayesian'"
                in sql
                for sql in normalized_sql
            )
        )
        self.assertTrue(
            any(
                "ALTER TABLE ingest_policy_runs ADD COLUMN IF NOT EXISTS n_trials INTEGER NOT NULL DEFAULT 0"
                in sql
                for sql in normalized_sql
            )
        )


if __name__ == "__main__":
    unittest.main()
