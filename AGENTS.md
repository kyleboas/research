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
- Detect eval: `python autoresearch_detect/eval_detect.py`
- Detect optimizer: `python autoresearch_detect/optimize_detect_policy.py`

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
