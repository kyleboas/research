import unittest
import json
import os
import tempfile
from pathlib import Path

from detect_policy import (
    DEFAULT_POLICY,
    compute_final_score,
    load_policy,
    novelty_adjustment,
    passes_report_gate,
    save_policy,
    source_diversity_adjustment,
)


class DetectPolicyTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("DETECT_POLICY_PATH", None)

    def test_novelty_adjustment_is_centered_on_half(self):
        self.assertEqual(novelty_adjustment(None), 0)
        self.assertEqual(novelty_adjustment(0.5), 0)
        self.assertGreater(novelty_adjustment(0.8), 0)
        self.assertLess(novelty_adjustment(0.2), 0)

    def test_source_diversity_penalizes_single_source_candidates(self):
        self.assertEqual(source_diversity_adjustment(1), -12)
        self.assertEqual(source_diversity_adjustment(3), 3)  # few_sources_bonus
        self.assertEqual(source_diversity_adjustment(6), 2)  # several_sources_bonus
        self.assertEqual(source_diversity_adjustment(12), -6)

    def test_compute_final_score_combines_all_signals(self):
        self.assertEqual(
            compute_final_score(
                base_score=50,
                novelty_score=0.7,  # (0.7 - 0.5) * 30 = +6
                feedback_adjustment=5,
                source_diversity=3,  # few_sources_bonus = +3
            ),
            64,  # 50 + 6 + 5 + 3 = 64
        )

    def test_report_gate_requires_score_and_source_support(self):
        self.assertTrue(
            passes_report_gate(final_score=67, source_diversity=3, min_score=40, min_sources=2)
        )
        self.assertFalse(
            passes_report_gate(final_score=67, source_diversity=1, min_score=40, min_sources=2)
        )
        self.assertFalse(
            passes_report_gate(final_score=35, source_diversity=3, min_score=40, min_sources=2)
        )

    def test_load_policy_reads_overrides_from_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            policy_path.write_text(json.dumps({"novelty_weight": 40, "report_min_score": 55}))
            os.environ["DETECT_POLICY_PATH"] = str(policy_path)

            policy = load_policy()

        self.assertEqual(policy["novelty_weight"], 40)
        self.assertEqual(policy["report_min_score"], 55)
        self.assertEqual(policy["report_min_sources"], DEFAULT_POLICY["report_min_sources"])

    def test_save_policy_writes_merged_policy_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            os.environ["DETECT_POLICY_PATH"] = str(policy_path)

            saved_path = save_policy({"single_source_penalty": -20})
            payload = json.loads(saved_path.read_text())

        self.assertEqual(payload["single_source_penalty"], -20)
        self.assertEqual(payload["few_sources_bonus"], DEFAULT_POLICY["few_sources_bonus"])

    def test_report_gate_uses_policy_defaults_when_not_explicitly_passed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            policy_path.write_text(json.dumps({"report_min_score": 55, "report_min_sources": 3}))
            os.environ["DETECT_POLICY_PATH"] = str(policy_path)

            self.assertTrue(passes_report_gate(final_score=60, source_diversity=3))
            self.assertFalse(passes_report_gate(final_score=54, source_diversity=3))
            self.assertFalse(passes_report_gate(final_score=60, source_diversity=2))


if __name__ == "__main__":
    unittest.main()
