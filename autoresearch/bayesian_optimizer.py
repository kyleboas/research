"""Bayesian optimization framework for autoresearch policy tuning.

Provides intelligent search strategies using Optuna with:
- Early stopping for unpromising configurations
- Warm-start from previous results
- Configurable acquisition functions (EI, PI, UCB)
- Budget-aware optimization
"""

from __future__ import annotations

import gc
import json
import os
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None

THREAD_LIMIT_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
)


def configure_constrained_runtime(thread_limit: int = 1) -> None:
    """Keep shared CPU usage predictable on constrained deployments."""
    for env_name in THREAD_LIMIT_ENV_VARS:
        os.environ.setdefault(env_name, str(max(1, int(thread_limit))))


configure_constrained_runtime()

try:
    import optuna
    from optuna.exceptions import TrialPruned
    from optuna.pruners import MedianPruner
    from optuna.samplers import CmaEsSampler, TPESampler

    OPTUNA_AVAILABLE = True
    OPTUNA_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:
    OPTUNA_AVAILABLE = False
    OPTUNA_IMPORT_ERROR = exc


@dataclass
class OptimizationConfig:
    """Configuration for Bayesian optimization."""

    n_trials: int = 100
    timeout_seconds: float | None = None
    n_startup_trials: int = 10
    acquisition_function: str = "ei"  # ei, pi, ucb
    beta_for_ucb: float = 2.0  # Exploration factor for UCB
    early_stopping: bool = True
    n_warmup_trials: int = 5  # Trials before pruning kicks in
    prune_threshold: float = 0.1  # Prune if worse than median by this margin
    study_name: str | None = None
    storage_path: str | None = None
    seed: int | None = None
    thread_limit: int = 1
    gc_after_trial: bool = True
    show_progress_bar: bool = False
    memory_soft_limit_mb: int | None = 384
    storage_soft_limit_mb: int | None = 32
    max_reported_trials: int = 200


def clone_optimization_config(config: OptimizationConfig) -> OptimizationConfig:
    """Return an isolated config instance so presets stay immutable."""
    return replace(config)


class BayesianOptimizer:
    """Generic Bayesian optimizer using Optuna for policy search."""

    def __init__(self, config: OptimizationConfig | None = None):
        if not OPTUNA_AVAILABLE:
            detail = f": {OPTUNA_IMPORT_ERROR}" if OPTUNA_IMPORT_ERROR else ""
            raise ImportError(
                "Optuna is unavailable. Install with: pip install optuna" + detail
            ) from OPTUNA_IMPORT_ERROR
        self.config = config or OptimizationConfig()
        self.study: optuna.Study | None = None
        self._trial_count = 0
        self._intermediate_results: list[dict] = []
        self._stop_reason: str | None = None
        configure_constrained_runtime(self.config.thread_limit)

    def _get_sampler(self):
        """Configure sampler based on acquisition function preference."""
        if self.config.acquisition_function == "cmaes":
            return CmaEsSampler(seed=self.config.seed)

        return TPESampler(
            n_startup_trials=self.config.n_startup_trials,
            seed=self.config.seed,
        )

    def _get_pruner(self):
        """Configure pruner for early stopping."""
        if not self.config.early_stopping:
            return optuna.pruners.NopPruner()

        return MedianPruner(
            n_startup_trials=self.config.n_warmup_trials,
            n_warmup_steps=0,
        )

    def create_study(
        self,
        direction: str = "maximize",
        study_name: str | None = None,
        storage_path: str | None = None,
    ) -> optuna.Study:
        """Create or load an existing study."""
        name = (
            study_name
            or self.config.study_name
            or f"policy_optimization_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
        )

        storage = None
        if storage_path or self.config.storage_path:
            db_path = Path(storage_path or self.config.storage_path)
            self._prepare_storage_path(db_path)
            storage = f"sqlite:///{db_path}"

        try:
            self.study = optuna.load_study(
                study_name=name,
                storage=storage,
                sampler=self._get_sampler(),
                pruner=self._get_pruner(),
            )
        except (KeyError, ValueError):
            self.study = optuna.create_study(
                study_name=name,
                storage=storage,
                direction=direction,
                sampler=self._get_sampler(),
                pruner=self._get_pruner(),
            )

        return self.study

    def _prepare_storage_path(self, db_path: Path) -> None:
        """Vacuum or reset oversized SQLite caches before Optuna opens them."""
        if self.config.storage_soft_limit_mb is None:
            return

        db_path.parent.mkdir(parents=True, exist_ok=True)
        if not db_path.exists():
            return

        soft_limit_bytes = int(self.config.storage_soft_limit_mb * 1024 * 1024)
        if db_path.stat().st_size <= soft_limit_bytes:
            return

        self._vacuum_sqlite(db_path)
        if db_path.exists() and db_path.stat().st_size > soft_limit_bytes:
            db_path.unlink()

    @staticmethod
    def _vacuum_sqlite(db_path: Path) -> None:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute("VACUUM")
        except sqlite3.DatabaseError:
            db_path.unlink(missing_ok=True)

    def _current_rss_mb(self) -> float | None:
        if psutil is None:
            return None
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)

    def _post_trial_callback(self, study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        if self.config.gc_after_trial:
            gc.collect()

        rss_mb = self._current_rss_mb()
        if (
            rss_mb is not None
            and self.config.memory_soft_limit_mb is not None
            and rss_mb > self.config.memory_soft_limit_mb
        ):
            self._stop_reason = (
                f"memory_soft_limit_reached:{rss_mb:.1f}MB>{self.config.memory_soft_limit_mb}MB"
            )
            study.stop()

    def optimize(
        self,
        objective_fn: Callable[[optuna.Trial], float],
        search_space: dict[str, tuple],
        n_trials: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run Bayesian optimization over the given search space."""
        if self.study is None:
            self.create_study()

        def wrapped_objective(trial: optuna.Trial) -> float:
            self._trial_count += 1

            params = {}
            for name, spec in search_space.items():
                param_type = spec[0]
                if param_type == "int":
                    params[name] = trial.suggest_int(name, spec[1], spec[2])
                elif param_type == "float":
                    params[name] = trial.suggest_float(name, spec[1], spec[2])
                elif param_type == "categorical":
                    params[name] = trial.suggest_categorical(name, spec[1])
                elif param_type == "int_step":
                    params[name] = trial.suggest_int(name, spec[1], spec[2], step=spec[3])
                elif param_type == "float_step":
                    params[name] = trial.suggest_float(name, spec[1], spec[2], step=spec[3])
                else:
                    raise ValueError(f"Unknown param type: {param_type}")

            try:
                result = objective_fn(trial, params)
                if isinstance(result, tuple):
                    score = result[0]
                    intermediate = result[1] if len(result) > 1 else None
                    if intermediate:
                        for step, value in enumerate(intermediate):
                            trial.report(value, step)
                            if trial.should_prune():
                                raise TrialPruned()
                else:
                    score = result

                self._intermediate_results.append(
                    {
                        "trial": self._trial_count,
                        "params": params,
                        "score": score,
                        "pruned": False,
                    }
                )
                return score
            except TrialPruned:
                self._intermediate_results.append(
                    {
                        "trial": self._trial_count,
                        "params": params,
                        "score": None,
                        "pruned": True,
                    }
                )
                raise

        self.study.optimize(
            wrapped_objective,
            n_trials=n_trials or self.config.n_trials,
            timeout=timeout or self.config.timeout_seconds,
            n_jobs=1,
            gc_after_trial=self.config.gc_after_trial,
            show_progress_bar=self.config.show_progress_bar,
            callbacks=[self._post_trial_callback],
            catch=(Exception,),
        )

        complete_trials = [
            t for t in self.study.trials if t.state == optuna.trial.TrialState.COMPLETE
        ]
        pruned_trials = [
            t for t in self.study.trials if t.state == optuna.trial.TrialState.PRUNED
        ]
        failed_trials = [
            t for t in self.study.trials if t.state == optuna.trial.TrialState.FAIL
        ]
        trials = self.study.trials
        if len(trials) > self.config.max_reported_trials:
            trials = trials[-self.config.max_reported_trials :]

        return {
            "best_params": self.study.best_params if complete_trials else {},
            "best_value": self.study.best_value if complete_trials else None,
            "n_trials": len(self.study.trials),
            "n_complete": len(complete_trials),
            "n_pruned": len(pruned_trials),
            "n_failed": len(failed_trials),
            "stop_reason": self._stop_reason,
            "trials": [
                {
                    "number": t.number,
                    "value": t.value,
                    "params": t.params,
                    "state": t.state.name,
                }
                for t in trials
            ],
        }

    def get_importance(self) -> dict[str, float]:
        """Get parameter importance from completed trials."""
        if self.study is None or len(self.study.trials) < 10:
            return {}

        try:
            importance = optuna.importance.get_param_importances(self.study)
            return dict(importance)
        except Exception:
            return {}

    def warm_start_from_results(self, previous_results: list[dict]) -> None:
        """Warm-start the optimizer from previous optimization results."""
        if self.study is None:
            self.create_study()

        for result in previous_results:
            if "params" in result and "value" in result:
                self.study.add_trial(
                    optuna.create_trial(
                        params=result["params"],
                        distributions=self._infer_distributions(result["params"]),
                        value=result["value"],
                    )
                )

    def _infer_distributions(self, params: dict) -> dict:
        """Infer parameter distributions from values."""
        distributions = {}
        for name, value in params.items():
            if isinstance(value, int):
                distributions[name] = optuna.distributions.IntDistribution(
                    low=value - 50, high=value + 50
                )
            elif isinstance(value, float):
                distributions[name] = optuna.distributions.FloatDistribution(
                    low=value - 10.0, high=value + 10.0
                )
            else:
                distributions[name] = optuna.distributions.CategoricalDistribution([value])
        return distributions

    def save_study(self, path: str | Path) -> None:
        """Save study state for later resumption."""
        if self.study is None:
            return

        save_data = {
            "study_name": self.study.study_name,
            "trials": [
                {
                    "number": t.number,
                    "value": t.value,
                    "params": t.params,
                    "state": t.state.name,
                }
                for t in self.study.trials
            ],
            "config": {
                "n_trials": self.config.n_trials,
                "acquisition_function": self.config.acquisition_function,
            },
        }

        Path(path).write_text(json.dumps(save_data, indent=2))

    def load_study(self, path: str | Path) -> None:
        """Load study state and recreate trials."""
        data = json.loads(Path(path).read_text())

        if self.study is None:
            self.create_study(study_name=data.get("study_name"))

        for trial_data in data.get("trials", []):
            if trial_data.get("value") is not None:
                self.study.add_trial(
                    optuna.create_trial(
                        params=trial_data["params"],
                        distributions=self._infer_distributions(trial_data["params"]),
                        value=trial_data["value"],
                    )
                )


def suggest_with_constraints(
    trial: optuna.Trial,
    name: str,
    spec: tuple,
    constraints: list[Callable[[dict], bool]] | None = None,
    max_attempts: int = 100,
) -> Any:
    """Suggest a parameter value with optional constraints."""
    param_type = spec[0]

    for _attempt in range(max_attempts):
        if param_type == "int":
            value = trial.suggest_int(name, spec[1], spec[2])
        elif param_type == "float":
            value = trial.suggest_float(name, spec[1], spec[2])
        elif param_type == "categorical":
            value = trial.suggest_categorical(name, spec[1])
        elif param_type == "int_step":
            value = trial.suggest_int(name, spec[1], spec[2], step=spec[3])
        elif param_type == "float_step":
            value = trial.suggest_float(name, spec[1], spec[2], step=spec[3])
        else:
            raise ValueError(f"Unknown param type: {param_type}")

        if constraints is None:
            return value

        trial_params = {name: value}
        if all(constraint(trial_params) for constraint in constraints):
            return value

    if param_type == "categorical":
        return spec[1][0]
    return spec[1]


def make_objective_with_budget(
    base_objective: Callable[[dict], float],
    cost_fn: Callable[[dict], float],
    budget_limit: float,
    penalty_weight: float = 10.0,
) -> Callable[[optuna.Trial, dict], float]:
    """Wrap an objective function with budget constraints."""

    def objective(trial: optuna.Trial, params: dict) -> float:
        del trial
        cost = cost_fn(params)
        base_score = base_objective(params)

        if cost > budget_limit:
            overage_ratio = cost / budget_limit
            penalty = penalty_weight * (overage_ratio - 1.0)
            return base_score - penalty

        efficiency_bonus = 0.0
        if cost < budget_limit * 0.8:
            efficiency_bonus = 0.5 * (1.0 - cost / budget_limit)

        return base_score + efficiency_bonus

    return objective


OPTIMIZATION_PRESETS = {
    "fast": OptimizationConfig(
        n_trials=30,
        timeout_seconds=300,
        n_startup_trials=5,
        early_stopping=True,
        n_warmup_trials=3,
    ),
    "thorough": OptimizationConfig(
        n_trials=200,
        n_startup_trials=20,
        early_stopping=True,
        n_warmup_trials=10,
    ),
    "budget_constrained": OptimizationConfig(
        n_trials=50,
        timeout_seconds=600,
        n_startup_trials=10,
        early_stopping=True,
        n_warmup_trials=5,
        acquisition_function="ei",
    ),
    "exploration": OptimizationConfig(
        n_trials=100,
        n_startup_trials=15,
        early_stopping=False,
        acquisition_function="ucb",
        beta_for_ucb=3.0,
    ),
}
