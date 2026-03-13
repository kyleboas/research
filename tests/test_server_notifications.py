import unittest

from server import (
    _format_detect_candidates_notification,
    _format_eval_notification,
    _format_optimize_notification,
    _format_report_benchmark_notification,
    _format_report_eval_notification,
    _format_report_optimize_notification,
    _parse_eval_summary,
    _parse_optimize_summary,
    _parse_report_benchmark_summary,
    _parse_report_eval_summary,
)


class ServerNotificationTests(unittest.TestCase):
    def test_parse_eval_summary_extracts_scores(self):
        summary = _parse_eval_summary(
            "\n".join(
                [
                    "fixture=/tmp/candidates.json",
                    "policy=/tmp/policy.json",
                    "precision_at_3=0.6667",
                    "pairwise_accuracy=0.7500",
                    "gate_accuracy=0.8000",
                    "report_recall=1.0000",
                    "FINAL_SCORE=92.50",
                ]
            )
        )

        self.assertEqual(summary["fixture"], "/tmp/candidates.json")
        self.assertEqual(summary["policy"], "/tmp/policy.json")
        self.assertEqual(summary["top_k"], 3)
        self.assertEqual(summary["precision_at_k"], 0.6667)
        self.assertEqual(summary["pairwise_accuracy"], 0.75)
        self.assertEqual(summary["gate_accuracy"], 0.8)
        self.assertEqual(summary["report_recall"], 1.0)
        self.assertEqual(summary["final_score"], 92.5)

    def test_format_eval_notification_is_concise(self):
        message = _format_eval_notification(
            {
                "top_k": 3,
                "precision_at_k": 0.6667,
                "gate_accuracy": 0.8,
                "report_recall": 1.0,
                "final_score": 92.5,
            }
        )

        self.assertIn("Detect policy eval finished.", message)
        self.assertIn("Final score: 92.50", message)
        self.assertIn("Precision@3: 0.6667", message)
        self.assertIn("Gate accuracy: 0.8000", message)
        self.assertIn("Report recall: 1.0000", message)

    def test_parse_optimize_summary_extracts_delta_and_policy(self):
        summary = _parse_optimize_summary(
            "\n".join(
                [
                    "fixture=/tmp/live_candidates.json",
                    "policy_path=/tmp/detect_policy_config.json",
                    "baseline=88.50",
                    "best=92.50",
                    "delta=4.00",
                    'best_policy={"novelty_weight": 35, "report_min_score": 40}',
                    "applied_policy=/tmp/detect_policy_config.json",
                ]
            )
        )

        self.assertEqual(summary["fixture"], "/tmp/live_candidates.json")
        self.assertEqual(summary["policy_path"], "/tmp/detect_policy_config.json")
        self.assertEqual(summary["baseline"], 88.5)
        self.assertEqual(summary["best"], 92.5)
        self.assertEqual(summary["delta"], 4.0)
        self.assertEqual(summary["best_policy"]["novelty_weight"], 35)
        self.assertEqual(summary["applied_policy"], "/tmp/detect_policy_config.json")

    def test_format_optimize_notification_reports_policy_change(self):
        message = _format_optimize_notification(
            {"baseline": 88.5, "best": 92.5, "delta": 4.0},
            policy_changed=True,
        )

        self.assertIn("Detect policy optimize finished.", message)
        self.assertIn("Score: 88.50 -> 92.50", message)
        self.assertIn("Delta: +4.00", message)
        self.assertIn("Policy changed: yes", message)

    def test_parse_report_eval_summary_extracts_scores(self):
        summary = _parse_report_eval_summary(
            "\n".join(
                [
                    "fixture=/tmp/reports.json",
                    "policy=/tmp/report_policy_config.json",
                    "average_item_score=78.25",
                    "section_coverage=0.8750",
                    "citation_validity=0.9500",
                    "citation_density=0.7000",
                    "source_diversity=0.6667",
                    "sources_section_coverage=0.8000",
                    "counterevidence_coverage=0.5000",
                    "thoroughness=0.7200",
                    "FINAL_SCORE=78.25",
                ]
            )
        )

        self.assertEqual(summary["fixture"], "/tmp/reports.json")
        self.assertEqual(summary["policy"], "/tmp/report_policy_config.json")
        self.assertEqual(summary["average_item_score"], 78.25)
        self.assertEqual(summary["section_coverage"], 0.875)
        self.assertEqual(summary["citation_validity"], 0.95)
        self.assertEqual(summary["final_score"], 78.25)

    def test_format_report_eval_notification_is_concise(self):
        message = _format_report_eval_notification(
            {
                "average_item_score": 78.25,
                "section_coverage": 0.875,
                "citation_validity": 0.95,
                "thoroughness": 0.72,
                "final_score": 78.25,
            }
        )

        self.assertIn("Report quality eval finished.", message)
        self.assertIn("Final score: 78.25", message)
        self.assertIn("Average item score: 78.25", message)
        self.assertIn("Citation validity: 0.9500", message)
        self.assertIn("Section coverage: 0.8750", message)
        self.assertIn("Thoroughness: 0.7200", message)

    def test_parse_report_benchmark_summary_extracts_delta_and_policy(self):
        summary = _parse_report_benchmark_summary(
            "\n".join(
                [
                    "fixture=/tmp/reports.json",
                    "policy_path=/tmp/report_policy_config.json",
                    "baseline=78.25",
                    "best=82.50",
                    "delta=4.25",
                    "estimated_cost_per_report=0.85",
                    "quality_per_dollar=97.0588",
                    "max_report_llm_cost_usd=1.00",
                    "budget_status=within_budget",
                    "min_improvement=1.00",
                    "policy_changed=yes",
                    "apply_decision=applied",
                    'best_policy={"max_research_rounds": 3, "subagent_search_limit": 30}',
                    "applied_policy=/tmp/report_policy_config.json",
                ]
            )
        )

        self.assertEqual(summary["fixture"], "/tmp/reports.json")
        self.assertEqual(summary["policy_path"], "/tmp/report_policy_config.json")
        self.assertEqual(summary["baseline"], 78.25)
        self.assertEqual(summary["best"], 82.5)
        self.assertEqual(summary["delta"], 4.25)
        self.assertEqual(summary["estimated_cost_per_report"], 0.85)
        self.assertEqual(summary["quality_per_dollar"], 97.0588)
        self.assertEqual(summary["max_report_llm_cost_usd"], 1.0)
        self.assertEqual(summary["budget_status"], "within_budget")
        self.assertEqual(summary["min_improvement"], 1.0)
        self.assertTrue(summary["policy_changed"])
        self.assertEqual(summary["apply_decision"], "applied")
        self.assertEqual(summary["best_policy"]["max_research_rounds"], 3)
        self.assertEqual(summary["applied_policy"], "/tmp/report_policy_config.json")

    def test_format_report_benchmark_notification_reports_policy_change(self):
        message = _format_report_benchmark_notification(
            {"baseline": 78.25, "best": 82.5, "delta": 4.25},
            policy_changed=True,
        )

        self.assertIn("Report policy benchmark finished.", message)
        self.assertIn("Score: 78.25 -> 82.50", message)
        self.assertIn("Delta: +4.25", message)
        self.assertIn("Policy changed: yes", message)

    def test_format_report_optimize_notification_reports_policy_change(self):
        message = _format_report_optimize_notification(
            {
                "baseline": 78.25,
                "best": 82.5,
                "delta": 4.25,
                "estimated_cost_per_report": 0.85,
                "max_report_llm_cost_usd": 1.0,
                "budget_status": "within_budget",
                "min_improvement": 1.0,
                "apply_decision": "applied",
            },
            policy_changed=True,
        )

        self.assertIn("Report policy optimize finished.", message)
        self.assertIn("Score: 78.25 -> 82.50", message)
        self.assertIn("Delta: +4.25", message)
        self.assertIn("Est. cost/report: $0.85", message)
        self.assertIn("Budget/report: $1.00", message)
        self.assertIn("Budget status: within_budget", message)
        self.assertIn("Min improvement: 1.00", message)
        self.assertIn("Policy changed: yes", message)
        self.assertIn("Apply decision: applied", message)

    def test_format_detect_candidates_notification_limits_lines(self):
        candidates = [
            {"id": 12, "trend": "High press rotation", "score": 71, "novelty_score": 0.63, "source_diversity": 3},
            {"id": 13, "trend": "Back-three rest defense", "score": 67, "novelty_score": 0.51, "source_diversity": 2},
        ]

        message = _format_detect_candidates_notification(candidates)

        self.assertIn("Detect found 2 new trend(s).", message)
        self.assertIn("#12 High press rotation | score=71 | novelty=0.63 | sources=3", message)
        self.assertIn("#13 Back-three rest defense | score=67 | novelty=0.51 | sources=2", message)


if __name__ == "__main__":
    unittest.main()
