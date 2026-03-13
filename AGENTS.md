# Agent Guide

This repo is easiest to work in when you treat it as four surfaces:

1. `main.py`: CLI entrypoint and top-level pipeline orchestration.
2. `server.py` + `dashboard.html`: dashboard and step runner.
3. Detect stack: `detect_*.py`, `trend_detection.py`, `novelty_scoring.py`, `tactical_extraction.py`, `detect_policy.py`.
4. Report stack: multi-agent research flow inside `main.py`.

Start with [`docs/repo-map.md`](docs/repo-map.md) before making structural edits.

## Fast Commands

Use `make help` for the full list. Most common commands:

- `make test-detect`
- `make eval-detect`
- `make dashboard`
- `make step-detect`
- `make step-rescore`

If `.venv/bin/python` exists, the Makefile uses it automatically.

## Entry Points

- CLI: `python main.py --step <ingest|backfill|detect|rescore|report|all>`
- Dashboard: `python server.py`
- Detect eval: `python autoresearch/detect/eval_detect.py`
- Detect optimizer: `python autoresearch/detect/optimize_detect_policy.py`

## Bayesian Optimization Framework

The autoresearch pipeline now uses intelligent Bayesian optimization instead of brute-force grid search. This provides significant efficiency gains while respecting the $5 Railway budget constraint.

### Core Module

`autoresearch/bayesian_optimizer.py` - Generic Bayesian optimization using Optuna with:
- **Early stopping**: Pruner terminates unpromising trials early (MedianPruner)
- **Warm-start**: Resume from previous optimization results
- **Parameter importance**: Shows which parameters most affect performance
- **Budget-aware**: Penalizes configurations that exceed cost limits

### Policy Optimizers

All three optimizers use Bayesian search by default:

**Detect Policy** (`autoresearch/detect/optimize_detect_policy.py`):
```bash
# Fast optimization (30 trials)
python autoresearch/detect/optimize_detect_policy.py --preset fast

# Thorough search (200 trials)
python autoresearch/detect/optimize_detect_policy.py --preset thorough

# With warm-start from previous results
python autoresearch/detect/optimize_detect_policy.py --warm-start
```
Replaces 12,800+ exhaustive grid combinations with intelligent sampling.

**Report Policy** (`autoresearch/report/optimize_report_policy.py`):
```bash
# Fast optimization (default, Railway-safe)
python autoresearch/report/optimize_report_policy.py

# Budget-constrained optimization
python autoresearch/report/optimize_report_policy.py --preset budget_constrained

# Use recent reports for simulation
python autoresearch/report/optimize_report_policy.py --limit 3 --refresh-auto
```
Replaces 4 hand-crafted policies with systematic policy space exploration.

**Ingest Policy** (`autoresearch/ingest/optimize_ingest_policy.py`):
```bash
# Analyze last 30 days of ingestion data
python autoresearch/ingest/optimize_ingest_policy.py --lookback-days 30
```
Optimizes overlap windows and detection thresholds based on historical lag patterns.

### Optimization Presets

- `fast` (30 trials, 5 minute timeout): Default Railway-safe setting for hourly runs
- `thorough` (200 trials): Comprehensive search for production
- `budget_constrained` (50 trials): Prioritizes cost-efficient configurations
- `exploration` (100 trials, no early stopping): Maximum parameter space coverage
- Runtime guardrails: single-threaded BLAS, post-trial GC, SQLite cache size limit, and a soft RSS stop condition

### Legacy Mode

For backward compatibility, all optimizers support the original implementations:
```bash
python autoresearch/detect/optimize_detect_policy.py --legacy
python autoresearch/report/optimize_report_policy.py --legacy
python autoresearch/ingest/optimize_ingest_policy.py --legacy
```

### Features

- **No LLM re-runs**: detect/ingest use policy evaluation only; report uses no-LLM simulations
- **Early stopping**: Trials pruned if intermediate results are unpromising
- **Warm-start**: Loads previous results to accelerate convergence
- **Study persistence**: SQLite storage allows resuming interrupted optimizations
- **Parameter importance**: Identifies which knobs matter most

## Where To Edit

For ingest changes:
- `main.py`
- `article_extractor.py`
- `feeds/rss.md`
- `feeds/youtube.md`

For detect changes:
- `detect_detectors.py`
- `detect_scoring.py`
- `detect_persistence.py`
- `detect_orchestration.py`
- `trend_detection.py`
- `novelty_scoring.py`
- `detect_policy.py`

For report-generation changes:
- `main.py`

For dashboard/API changes:
- `server.py`
- `dashboard.html`

For schema/storage changes:
- `sql/schema.sql`
- `db_conn.py`

## Conventions

- Keep `main.py` as a compatibility/orchestration surface. Prefer extracting new logic into focused modules instead of expanding `main.py`.
- Preserve CLI step names and existing dashboard API contracts unless the change explicitly intends to break them.
- Treat `trend_candidates.status`, `novelty_score`, `final_score`, and `source_diversity` as production-sensitive fields. Migration order matters.
- Detect changes should usually be verified with both:
  - `make test-detect`
  - `make eval-detect`
- If a change touches step execution or dashboard wiring, also run:
  - `python main.py --help`

## Safety Notes

- This repo may already have an open PR branch. Check `git status` and `git branch --show-current` before stacking changes.
- Do not assume production config exists locally. DB-backed flows often require Railway env vars.
- Avoid broad file moves unless they produce a clear improvement in navigability and import boundaries.
