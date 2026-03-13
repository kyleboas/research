import unittest

from autoresearch_report.benchmark_report import (
    policy_changed,
    report_policy_apply_decision,
)


class ReportPolicyOptimizerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
