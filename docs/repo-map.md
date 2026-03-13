# Repo Map

This file is a fast orientation pass for coding agents.

## Top-Level Layout

- `main.py`: main CLI pipeline entrypoint.
- `server.py`: HTTP dashboard and step runner.
- `dashboard.html`: single-page dashboard UI.
- `detect_detectors.py`: detect-stage candidate generation and LLM fallback.
- `detect_scoring.py`: novelty enrichment and feedback-based scoring helpers.
- `detect_persistence.py`: trend-candidate persistence and rescore storage helpers.
- `detect_orchestration.py`: detect/rescore orchestration.
- `trend_detection.py`: BERTrend-style weak-signal detection.
- `tactical_extraction.py`: structured tactical extraction.
- `novelty_scoring.py`: baseline-aware novelty scoring.
- `detect_policy.py`: score policy and report gate math.
- `article_extractor.py`: full-text extraction helper for RSS articles.
- `db_conn.py`: database connection resolution.

## Operational Files

- `env.example`: required env vars and runtime knobs.
- `config.json`: model and pipeline config defaults.
- `railway.toml`: Railway deployment config.
- `sql/schema.sql`: primary schema and production migration path.
- `sql/migrate_multilang.sql`: older upgrade path for multilingual support.

## Detect Surface

The detect layer now has explicit boundaries:

- `detect_detectors.py`
  - BERTrend + tactical pattern candidate generation
  - LLM-only fallback
  - candidate deduplication
- `detect_scoring.py`
  - keyword/semantic feedback adjustments
  - novelty enrichment for candidates missing novelty data
- `detect_persistence.py`
  - trend fingerprinting
  - upsert logic
  - historical rescore persistence helpers
- `detect_orchestration.py`
  - step-level `run_detect`
  - step-level `run_rescore`

`main.py` still exposes wrapper functions so existing tests/CLI surfaces remain stable.

## Report Surface

The report-generation flow is still concentrated in `main.py`:

- topic decomposition
- parallel subagents
- synthesis
- sufficiency evaluation
- citation verification
- final revision

If that area is refactored later, do it as a separate boundary change. Do not mix it with detect work.

## Tests

- `tests/test_pipeline_helpers.py`: helper behavior used by pipeline/detect persistence.
- `tests/test_novelty_scoring.py`: novelty behavior.
- `tests/test_detect_policy.py`: scoring policy behavior.
- `tests/test_detect_evaluator.py`: detect eval metrics behavior.

Offline eval tooling lives in `autoresearch_detect/`.

## Common Change Paths

If you are changing novelty behavior:
- start in `novelty_scoring.py`
- then inspect `detect_scoring.py`
- then run `make test-detect` and `make eval-detect`

If you are changing candidate persistence:
- start in `detect_persistence.py`
- then inspect `server.py` dashboard queries if UI-visible fields change
- then check `sql/schema.sql` if storage shape changes

If you are changing step execution:
- start in `main.py`
- then inspect `detect_orchestration.py`
- then inspect `server.py`

## Verification Shortlist

- `make test-detect`
- `make eval-detect`
- `python main.py --help`
- `python server.py`
