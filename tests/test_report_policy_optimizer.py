import unittest

from autoresearch.report.benchmark_report import (
    estimate_report_llm_cost,
    ensure_report_policy_runs_table,
    policy_changed,
    quality_per_dollar,
    report_policy_apply_decision,
)


class ReportPolicyOptimizerTests(unittest.TestCase):
    def test_ensure_report_policy_runs_table_backfills_budget_status_column(self):
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

        ensure_report_policy_runs_table(FakeConn())

        normalized_sql = [" ".join(sql.split()) for sql, _params in executed]
        self.assertTrue(
            any("ALTER TABLE report_policy_runs ADD COLUMN IF NOT EXISTS budget_status TEXT NOT NULL DEFAULT ''" in sql for sql in normalized_sql)
        )

    def test_policy_changed_detects_real_diff(self):
        base = {"max_research_rounds": 2, "subagent_search_limit": 24}
        candidate = {"max_research_rounds": 3, "subagent_search_limit": 24}

        self.assertTrue(policy_changed(base, candidate))
        self.assertFalse(policy_changed(base, dict(base)))

    def test_apply_decision_skips_when_policy_unchanged(self):
        applied, reason = report_policy_apply_decision(
            baseline_score=78.0,
            best_score=80.0,
            min_improvement=1.0,
            policy_changed=False,
        )

        self.assertFalse(applied)
        self.assertEqual(reason, "no_policy_change")

    def test_apply_decision_skips_when_improvement_below_threshold(self):
        applied, reason = report_policy_apply_decision(
            baseline_score=78.0,
            best_score=78.5,
            min_improvement=1.0,
            policy_changed=True,
        )

        self.assertFalse(applied)
        self.assertEqual(reason, "below_min_improvement")

    def test_apply_decision_applies_when_threshold_is_met(self):
        applied, reason = report_policy_apply_decision(
            baseline_score=78.0,
            best_score=79.25,
            min_improvement=1.0,
            policy_changed=True,
        )

        self.assertTrue(applied)
        self.assertEqual(reason, "applied")

    def test_estimated_cost_increases_with_larger_policy(self):
        smaller = estimate_report_llm_cost(
            {
                "moderate_min_tasks": 2,
                "max_research_rounds": 2,
                "subagent_max_tokens": 4000,
                "synthesis_max_tokens": 10000,
                "revision_max_tokens": 10000,
            }
        )
        larger = estimate_report_llm_cost(
            {
                "moderate_min_tasks": 4,
                "max_research_rounds": 3,
                "subagent_max_tokens": 7000,
                "synthesis_max_tokens": 16000,
                "revision_max_tokens": 16000,
            }
        )

        self.assertGreater(larger, smaller)

    def test_quality_per_dollar_prefers_cheaper_equal_quality(self):
        self.assertGreater(quality_per_dollar(80, 1.0), quality_per_dollar(80, 2.0))


if __name__ == "__main__":
    unittest.main()
