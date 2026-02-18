# Tasks: Enhanced Trend Selection

## Relevant Files

- `src/generation/trend_pass.py` - Main file to be rewritten; all six improvements live here.
- `src/generation/prompts.py` - `TREND_SYSTEM`, `TREND_USER_TEMPLATE`, and `build_trend_prompt()` must be updated for ranked-candidate JSON output.
- `src/pipeline.py` - Caller of `run_trend_pass()`; must catch `TrendPassError` and update metadata writes.
- `sql/001_init.sql` - Reference only (no schema change needed; `pipeline_runs.metadata` is already JSONB).
- `tests/test_trend_pass.py` - New test file covering all six improvement areas.

### Notes

- Unit tests should be placed alongside the code they test. The new test file goes in `tests/test_trend_pass.py`.
- Run tests with `npx jest` or `pytest tests/` depending on the project test runner. This project uses Python, so use `pytest tests/test_trend_pass.py`.
- No database migrations are required; `pipeline_runs.metadata` is already `JSONB DEFAULT '{}'`.

## Instructions for Completing Tasks

IMPORTANT: As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [ ] 0.0 Create feature branch
  - [ ] 0.1 Create and checkout branch `claude/enhance-trend-selection-aUWck` (already the working branch per session config)

- [ ] 1.0 Update prompt templates for ranked-candidate output
- [ ] 2.0 Refactor source-list building (snippet sizing, labels, relative age)
- [ ] 3.0 Implement ranked-candidate LLM call, JSON parsing, and candidate storage
- [ ] 4.0 Implement historical topic deduplication via semantic similarity
- [ ] 5.0 Implement adaptive lookback window (7 → 14 days) and metadata flagging
- [ ] 6.0 Implement output validation, re-prompt logic, and TrendPassError
- [ ] 7.0 Update pipeline.py to handle TrendPassError and write new metadata keys
- [ ] 8.0 Write tests
