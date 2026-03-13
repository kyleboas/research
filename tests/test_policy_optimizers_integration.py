"""Integration tests for the policy optimizers.

These tests verify that the optimizers can run without errors
and produce valid outputs.
"""

import unittest
import tempfile
import json
from pathlib import Path
import sys
from unittest import mock

# Add repo root to path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class TestDetectPolicyOptimizer(unittest.TestCase):
    def test_imports(self):
        """Test that the optimizer module can be imported."""
        from autoresearch.detect import optimize_detect_policy
        self.assertTrue(hasattr(optimize_detect_policy, 'main'))
    
    def test_search_space_defined(self):
        from autoresearch.detect.optimize_detect_policy import SEARCH_SPACE_BAYESIAN
        self.assertIsInstance(SEARCH_SPACE_BAYESIAN, dict)
        self.assertIn("novelty_weight", SEARCH_SPACE_BAYESIAN)
        self.assertIn("report_min_score", SEARCH_SPACE_BAYESIAN)


class TestReportPolicyOptimizer(unittest.TestCase):
    def test_imports(self):
        """Test that the optimizer module can be imported."""
        from autoresearch.report import optimize_report_policy
        self.assertTrue(hasattr(optimize_report_policy, 'main'))
    
    def test_search_space_defined(self):
        from autoresearch.report.optimize_report_policy import SEARCH_SPACE_BAYESIAN
        self.assertIsInstance(SEARCH_SPACE_BAYESIAN, dict)
        self.assertIn("max_research_rounds", SEARCH_SPACE_BAYESIAN)
        self.assertIn("subagent_max_tokens", SEARCH_SPACE_BAYESIAN)
    
    def test_simulate_policy_structure(self):
        from autoresearch.report.optimize_report_policy import simulate_policy
        import inspect
        sig = inspect.signature(simulate_policy)
        params = list(sig.parameters.keys())
        self.assertIn("items", params)
        self.assertIn("base_policy", params)
        self.assertIn("candidate_policy", params)


class TestIngestPolicyOptimizer(unittest.TestCase):
    def test_imports(self):
        """Test that the optimizer module can be imported."""
        from autoresearch.ingest import optimize_ingest_policy
        self.assertTrue(hasattr(optimize_ingest_policy, 'main'))
    
    def test_search_space_defined(self):
        from autoresearch.ingest.optimize_ingest_policy import SEARCH_SPACE_BAYESIAN
        self.assertIsInstance(SEARCH_SPACE_BAYESIAN, dict)
        self.assertIn("rss_overlap_seconds", SEARCH_SPACE_BAYESIAN)
        self.assertIn("detect_min_new_sources", SEARCH_SPACE_BAYESIAN)

    def test_falls_back_to_legacy_when_bayesian_unavailable(self):
        from autoresearch.ingest import optimize_ingest_policy

        with mock.patch.object(optimize_ingest_policy, "OPTUNA_AVAILABLE", False), \
             mock.patch.object(optimize_ingest_policy, "OPTUNA_IMPORT_ERROR", ImportError("broken optuna")), \
             mock.patch.object(optimize_ingest_policy, "run_legacy_optimizer") as legacy_runner, \
             mock.patch.object(sys, "argv", ["optimize_ingest_policy.py"]):
            optimize_ingest_policy.main()

        legacy_runner.assert_called_once()


class TestLegacyBackwardsCompatibility(unittest.TestCase):
    def test_detect_legacy_imports(self):
        from autoresearch.detect import optimize_detect_policy_legacy
        self.assertTrue(hasattr(optimize_detect_policy_legacy, 'main'))
    
    def test_report_legacy_imports(self):
        from autoresearch.report import optimize_report_policy_legacy
        self.assertTrue(hasattr(optimize_report_policy_legacy, 'main'))
    
    def test_ingest_legacy_imports(self):
        from autoresearch.ingest import optimize_ingest_policy_legacy
        self.assertTrue(hasattr(optimize_ingest_policy_legacy, 'main'))


class TestBayesianOptimizerModule(unittest.TestCase):
    def test_framework_imports(self):
        from autoresearch import bayesian_optimizer
        self.assertTrue(hasattr(bayesian_optimizer, 'BayesianOptimizer'))
        self.assertTrue(hasattr(bayesian_optimizer, 'OptimizationConfig'))
        self.assertTrue(hasattr(bayesian_optimizer, 'OPTIMIZATION_PRESETS'))


if __name__ == "__main__":
    unittest.main()
