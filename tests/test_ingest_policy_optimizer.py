import unittest

from autoresearch.ingest.optimize_ingest_policy import ensure_ingest_policy_runs_table, record_run


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

    def test_record_run_omits_missing_columns_when_schema_lags(self):
        executed = []

        class FakeCursor:
            def __init__(self):
                self.last_sql = ""

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                rendered = str(sql)
                self.last_sql = rendered
                executed.append((rendered, params))

            def fetchall(self):
                if "information_schema.columns" not in self.last_sql:
                    return []
                return [
                    ("baseline_score",),
                    ("best_score",),
                    ("delta",),
                    ("min_improvement",),
                    ("applied",),
                    ("apply_decision",),
                    ("observations",),
                    ("baseline_policy",),
                    ("best_policy",),
                ]

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                return None

        record_run(
            FakeConn(),
            baseline_policy={"rss_overlap_seconds": 86400},
            best_policy={"rss_overlap_seconds": 172800},
            observations={"rss_p90_lag_hours": 24},
            baseline_score=80.0,
            best_score=90.0,
            min_improvement=1.0,
            applied=True,
            apply_decision_value="applied",
            optimization_type="bayesian",
            n_trials=50,
        )

        insert_sql = next(sql for sql, _params in executed if "INSERT INTO ingest_policy_runs" in sql)
        self.assertNotIn("optimization_type", insert_sql)
        self.assertNotIn("n_trials", insert_sql)


if __name__ == "__main__":
    unittest.main()
