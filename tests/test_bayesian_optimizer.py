"""Tests for the Bayesian optimization framework."""

import os
import tempfile
import unittest
from pathlib import Path

from autoresearch.bayesian_optimizer import (
    BayesianOptimizer,
    OptimizationConfig,
    OPTIMIZATION_PRESETS,
    clone_optimization_config,
    configure_constrained_runtime,
    make_objective_with_budget,
)


class TestOptimizationConfig(unittest.TestCase):
    def test_default_config(self):
        config = OptimizationConfig()
        self.assertEqual(config.n_trials, 100)
        self.assertEqual(config.acquisition_function, "ei")
        self.assertTrue(config.early_stopping)
        self.assertEqual(config.memory_soft_limit_mb, 384)
        self.assertFalse(config.show_progress_bar)

    def test_presets_exist(self):
        self.assertIn("fast", OPTIMIZATION_PRESETS)
        self.assertIn("thorough", OPTIMIZATION_PRESETS)
        self.assertIn("budget_constrained", OPTIMIZATION_PRESETS)
        self.assertIn("exploration", OPTIMIZATION_PRESETS)

    def test_fast_preset(self):
        config = OPTIMIZATION_PRESETS["fast"]
        self.assertEqual(config.n_trials, 30)
        self.assertEqual(config.n_startup_trials, 5)
        self.assertEqual(config.timeout_seconds, 300)

    def test_clone_optimization_config(self):
        config = clone_optimization_config(OPTIMIZATION_PRESETS["fast"])
        config.n_trials = 99
        self.assertEqual(OPTIMIZATION_PRESETS["fast"].n_trials, 30)
        self.assertEqual(config.n_trials, 99)


class TestRuntimeGuards(unittest.TestCase):
    def test_configure_constrained_runtime_sets_missing_thread_limits(self):
        original = {name: os.environ.get(name) for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS")}
        try:
            os.environ.pop("OMP_NUM_THREADS", None)
            os.environ.pop("OPENBLAS_NUM_THREADS", None)
            configure_constrained_runtime(1)
            self.assertEqual(os.environ["OMP_NUM_THREADS"], "1")
            self.assertEqual(os.environ["OPENBLAS_NUM_THREADS"], "1")
        finally:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_storage_guard_deletes_oversized_sqlite_cache(self):
        optimizer = BayesianOptimizer.__new__(BayesianOptimizer)
        optimizer.config = OptimizationConfig(storage_soft_limit_mb=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "study.sqlite"
            db_path.write_bytes(b"not-a-real-sqlite-db")
            optimizer._prepare_storage_path(db_path)
            self.assertFalse(db_path.exists())

    def test_memory_guard_stops_study(self):
        optimizer = BayesianOptimizer.__new__(BayesianOptimizer)
        optimizer.config = OptimizationConfig(memory_soft_limit_mb=10, gc_after_trial=False)
        optimizer._stop_reason = None
        optimizer._current_rss_mb = lambda: 11.5

        class DummyStudy:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        study = DummyStudy()
        optimizer._post_trial_callback(study, None)
        self.assertTrue(study.stopped)
        self.assertIn("memory_soft_limit_reached", optimizer._stop_reason)


class TestBayesianOptimizer(unittest.TestCase):
    def test_optimizer_requires_optuna(self):
        try:
            optimizer = BayesianOptimizer()
            self.assertIsNotNone(optimizer.config)
        except ImportError:
            with self.assertRaises(ImportError):
                BayesianOptimizer()

    def test_warm_start_from_results(self):
        try:
            import optuna  # noqa: F401

            optimizer = BayesianOptimizer(OptimizationConfig(n_trials=10))
            optimizer.create_study(direction="maximize")
            previous_results = [
                {"params": {"x": 1.0, "y": 2.0}, "value": 10.0},
                {"params": {"x": 2.0, "y": 3.0}, "value": 20.0},
            ]
            optimizer.warm_start_from_results(previous_results)
            self.assertEqual(len(optimizer.study.trials), 2)
        except ImportError:
            self.skipTest("optuna not installed")

    def test_get_importance_empty(self):
        try:
            import optuna  # noqa: F401

            optimizer = BayesianOptimizer(OptimizationConfig(n_trials=10))
            optimizer.create_study(direction="maximize")
            importance = optimizer.get_importance()
            self.assertEqual(importance, {})
        except ImportError:
            self.skipTest("optuna not installed")


class TestMakeObjectiveWithBudget(unittest.TestCase):
    def test_budget_constraint_penalty(self):
        def base_objective(params):
            return 100.0

        def cost_fn(params):
            return 2.0

        objective = make_objective_with_budget(base_objective, cost_fn, 1.0)
        score = objective(None, {"x": 1.0})
        self.assertLess(score, 100.0)

    def test_budget_efficiency_bonus(self):
        def base_objective(params):
            return 50.0

        def cost_fn(params):
            return 0.5

        objective = make_objective_with_budget(base_objective, cost_fn, 1.0)
        score = objective(None, {"x": 1.0})
        self.assertGreater(score, 50.0)


class TestStudyPersistence(unittest.TestCase):
    def test_save_and_load_study(self):
        try:
            import optuna

            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "study.json"

                optimizer = BayesianOptimizer(OptimizationConfig(n_trials=10))
                optimizer.create_study(direction="maximize", study_name="test_study")
                optimizer.study.add_trial(
                    optuna.create_trial(
                        params={"x": 1.0},
                        distributions={"x": optuna.distributions.FloatDistribution(0, 10)},
                        value=5.0,
                    )
                )

                optimizer.save_study(path)

                optimizer2 = BayesianOptimizer(OptimizationConfig(n_trials=10))
                optimizer2.load_study(path)

                self.assertEqual(len(optimizer2.study.trials), 1)
                self.assertEqual(optimizer2.study.trials[0].value, 5.0)
        except ImportError:
            self.skipTest("optuna not installed")


if __name__ == "__main__":
    unittest.main()
