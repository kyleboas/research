import unittest
from datetime import UTC, datetime

from server import (
    _build_autoresearch_history,
    _format_autoresearch_hourly_notification,
    _format_detect_candidates_notification,
    _format_eval_notification,
    _format_ingest_policy_notification,
    _format_optimize_notification,
    _format_report_benchmark_notification,
    _format_report_eval_notification,
    _format_report_optimize_notification,
    _parse_autoresearch_hourly_summary,
    _parse_runtime_llm_summary,
    _parse_eval_summary,
    _parse_ingest_policy_summary,
    _parse_optimize_summary,
    _parse_report_benchmark_summary,
    _parse_report_eval_summary,
    _reconcile_persisted_step_run,
)


class ServerNotificationTests(unittest.TestCase):
    def test_reconcile_persisted_step_run_marks_old_running_run_as_failed(self):
        current = {
            "status": "idle",
            "started_at": None,
            "finished_at": None,
            "duration_seconds": None,
            "duration_human": None,
            "exit_code": None,
            "log_tail": "",
        }
        run = {
            "status": "running",
            "started_at": "2026-03-13T12:00:00+00:00",
            "finished_at": None,
            "duration_seconds": None,
            "duration_human": None,
            "exit_code": None,
            "summary": {"trigger_source": "cli"},
        }

        reconciled = _reconcile_persisted_step_run(
            "ingest",
            current,
            run,
            now=datetime(2026, 3, 13, 19, 30, tzinfo=UTC),
        )

        self.assertEqual(reconciled["status"], "failed")
        self.assertEqual(reconciled["duration_human"], "7h 30m 0s")
        self.assertIn("Marked stale after 7h 30m 0s", reconciled["log_tail"])
        self.assertIn('"trigger_source": "cli"', reconciled["log_tail"])

    def test_reconcile_persisted_step_run_keeps_fresh_running_run(self):
        current = {
            "status": "idle",
            "started_at": None,
            "finished_at": None,
            "duration_seconds": None,
            "duration_human": None,
            "exit_code": None,
            "log_tail": "",
        }
        run = {
            "status": "running",
            "started_at": "2026-03-13T12:00:00+00:00",
            "finished_at": None,
            "duration_seconds": None,
            "duration_human": None,
            "exit_code": None,
            "summary": {},
        }

        reconciled = _reconcile_persisted_step_run(
            "ingest",
            current,
            run,
            now=datetime(2026, 3, 13, 16, 0, tzinfo=UTC),
        )

        self.assertEqual(reconciled["status"], "running")
        self.assertEqual(reconciled["log_tail"], "")

    def test_build_autoresearch_history_shapes_scores_and_runtime(self):
        history = _build_autoresearch_history(
            [
                {
                    "id": 12,
                    "step": "autoresearch_hourly",
                    "status": "success",
                    "trigger_source": "autoresearch_pipeline",
                    "started_at": "2026-03-13T10:00:00+00:00",
                    "finished_at": "2026-03-13T10:02:18+00:00",
                    "duration_seconds": 138.4,
                    "summary": {
                        "detect_eval_score": "92.50",
                        "report_eval_score": "78.25",
                        "ingest_policy_delta": "8.25",
                        "detect_policy_delta": "1.75",
                        "report_policy_delta": "2.00",
                        "report_policy_apply_decision": "applied",
                    },
                }
            ]
        )

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], 12)
        self.assertEqual(history[0]["quality_index"], 85.38)
        self.assertEqual(history[0]["runtime_minutes"], 2.31)
        self.assertEqual(history[0]["duration_human"], "2m 19s")
        self.assertEqual(history[0]["detect_eval_score"], 92.5)
        self.assertEqual(history[0]["report_eval_score"], 78.25)
        self.assertEqual(history[0]["ingest_policy_delta"], 8.25)
        self.assertEqual(history[0]["detect_policy_delta"], 1.75)
        self.assertEqual(history[0]["report_policy_delta"], 2.0)
        self.assertEqual(history[0]["report_policy_apply_decision"], "applied")

    def test_build_autoresearch_history_ignores_other_steps_and_missing_scores(self):
        history = _build_autoresearch_history(
            [
                {
                    "id": 3,
                    "step": "detect",
                    "status": "success",
                    "trigger_source": "dashboard",
                    "started_at": "2026-03-13T09:00:00+00:00",
                    "finished_at": "2026-03-13T09:01:00+00:00",
                    "duration_seconds": 60,
                    "summary": {},
                },
                {
                    "id": 4,
                    "step": "autoresearch_hourly",
                    "status": "failed",
                    "trigger_source": "dashboard",
                    "started_at": "2026-03-13T11:00:00+00:00",
                    "finished_at": "2026-03-13T11:03:00+00:00",
                    "duration_seconds": None,
                    "summary": {
                        "total_duration_seconds": "180",
                        "report_eval_score": "81.50",
                    },
                },
            ]
        )

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], 4)
        self.assertEqual(history[0]["quality_index"], 81.5)
        self.assertEqual(history[0]["runtime_minutes"], 3.0)
        self.assertEqual(history[0]["duration_human"], "3m 0s")
        self.assertIsNone(history[0]["detect_eval_score"])
        self.assertEqual(history[0]["report_eval_score"], 81.5)

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

    def test_parse_ingest_policy_summary_extracts_delta_and_policy(self):
        summary = _parse_ingest_policy_summary(
            "\n".join(
                [
                    "policy_path=/tmp/ingest_policy_config.json",
                    "baseline=71.25",
                    "best=79.50",
                    "delta=8.25",
                    "min_improvement=1.00",
                    "policy_changed=yes",
                    "apply_decision=applied",
                    'best_policy={"rss_overlap_seconds": 86400, "youtube_overlap_seconds": 86400, "detect_min_new_sources": 2}',
                    "applied_policy=/tmp/ingest_policy_config.json",
                ]
            )
        )

        self.assertEqual(summary["policy_path"], "/tmp/ingest_policy_config.json")
        self.assertEqual(summary["baseline"], 71.25)
        self.assertEqual(summary["best"], 79.5)
        self.assertEqual(summary["delta"], 8.25)
        self.assertEqual(summary["min_improvement"], 1.0)
        self.assertTrue(summary["policy_changed"])
        self.assertEqual(summary["apply_decision"], "applied")
        self.assertEqual(summary["best_policy"]["detect_min_new_sources"], 2)

    def test_format_ingest_policy_notification_reports_policy_change(self):
        message = _format_ingest_policy_notification(
            {"baseline": 71.25, "best": 79.5, "delta": 8.25, "min_improvement": 1.0, "apply_decision": "applied"},
            policy_changed=True,
        )

        self.assertIn("Ingest policy optimize finished.", message)
        self.assertIn("Score: 71.25 -> 79.50", message)
        self.assertIn("Delta: +8.25", message)
        self.assertIn("Min improvement: 1.00", message)
        self.assertIn("Policy changed: yes", message)
        self.assertIn("Apply decision: applied", message)

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

    def test_parse_runtime_llm_summary_extracts_cost_fields(self):
        summary = _parse_runtime_llm_summary(
            "\n".join(
                [
                    "RUN_LLM_CALLS=9",
                    "RUN_PROMPT_TOKENS=12000",
                    "RUN_COMPLETION_TOKENS=3400",
                    "RUN_CACHED_PROMPT_TOKENS=2000",
                    "RUN_REASONING_TOKENS=800",
                    "RUN_TOTAL_TOKENS=15400",
                    "RUN_LLM_COST_USD=0.183420",
                    'RUN_UNPRICED_MODELS=["google/gemini-2.5-flash"]',
                ]
            )
        )

        self.assertEqual(summary["llm_calls"], 9)
        self.assertEqual(summary["prompt_tokens"], 12000)
        self.assertEqual(summary["completion_tokens"], 3400)
        self.assertEqual(summary["cached_prompt_tokens"], 2000)
        self.assertEqual(summary["reasoning_tokens"], 800)
        self.assertEqual(summary["total_tokens"], 15400)
        self.assertEqual(summary["llm_cost_usd"], 0.18342)
        self.assertEqual(summary["unpriced_models"], ["google/gemini-2.5-flash"])

    def test_parse_autoresearch_hourly_summary_extracts_step_results(self):
        summary = _parse_autoresearch_hourly_summary(
            "\n".join(
                [
                    "AUTORESEARCH_STATUS=success",
                    "AUTORESEARCH_TOTAL_COST_USD=0.000000",
                    "AUTORESEARCH_TOTAL_DURATION_SECONDS=138.40",
                    "ingest_policy_delta=8.25",
                    "detect_eval_score=92.50",
                    "detect_policy_delta=1.75",
                    "report_eval_score=78.25",
                    "report_policy_delta=2.00",
                    "report_policy_apply_decision=applied",
                    "ingest_policy_optimize_duration_seconds=12.00",
                    "detect_policy_eval_duration_seconds=24.10",
                    "detect_policy_optimize_duration_seconds=31.50",
                    "report_policy_eval_duration_seconds=29.20",
                    "report_policy_optimize_duration_seconds=41.60",
                ]
            )
        )

        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["llm_cost_usd"], 0.0)
        self.assertEqual(summary["total_duration_seconds"], 138.4)
        self.assertEqual(summary["ingest_policy_delta"], 8.25)
        self.assertEqual(summary["detect_eval_score"], 92.5)
        self.assertEqual(summary["detect_policy_delta"], 1.75)
        self.assertEqual(summary["report_eval_score"], 78.25)
        self.assertEqual(summary["report_policy_delta"], 2.0)
        self.assertEqual(summary["report_policy_apply_decision"], "applied")
        self.assertEqual(summary["ingest_policy_optimize_duration_seconds"], 12.0)
        self.assertEqual(summary["report_policy_optimize_duration_seconds"], 41.6)

    def test_format_autoresearch_hourly_notification_is_concise(self):
        message = _format_autoresearch_hourly_notification(
            {
                "total_duration_seconds": 138.4,
                "llm_cost_usd": 0.0,
                "detect_eval_score": 92.5,
                "detect_policy_delta": 1.75,
                "report_eval_score": 78.25,
                "report_policy_delta": 2.0,
                "report_policy_apply_decision": "applied",
                "ingest_policy_delta": 8.25,
                "ingest_policy_optimize_duration_seconds": 12.0,
                "detect_policy_eval_duration_seconds": 24.1,
                "detect_policy_optimize_duration_seconds": 31.5,
                "report_policy_eval_duration_seconds": 29.2,
                "report_policy_optimize_duration_seconds": 41.6,
            }
        )

        self.assertIn("Autoresearch hourly finished.", message)
        self.assertIn("Runtime: 2m 19s", message)
        self.assertIn("LLM cost: $0.0000", message)
        self.assertIn("Detect eval: 92.50", message)
        self.assertIn("Detect policy delta: +1.75", message)
        self.assertIn("Report eval: 78.25", message)
        self.assertIn("Report policy delta: +2.00", message)
        self.assertIn("Report apply decision: applied", message)
        self.assertIn("Ingest policy delta: +8.25", message)
        self.assertIn("Step runtimes: ingest 12.0s", message)
        self.assertIn("report tune 41.6s", message)


if __name__ == "__main__":
    unittest.main()
