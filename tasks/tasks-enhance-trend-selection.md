# Tasks: Enhanced Trend Selection

## Relevant Files

- `src/generation/trend_pass.py` - Primary file for all improvements: snippet sizing, source labels, ranked candidates, deduplication, adaptive window, validation, velocity scoring, and TrendPassError.
- `src/generation/prompts.py` - `TREND_SYSTEM`, `TREND_USER_TEMPLATE`, `TREND_REPROMPT_USER_TEMPLATE`, and `build_trend_prompt()` / `build_trend_reprompt()` must be updated.
- `src/pipeline.py` - Caller of `run_trend_pass()`; must handle the new `TrendPassResult` return type, catch `TrendPassError`, and write `trend_candidates` / `trend_lookback_days` / `dedup_max_similarity` into `pipeline_runs.metadata`.
- `src/processing/embeddings.py` - Reference only. Contains `_embed_batch()` and the OpenAI client setup that the new `_embed_texts()` helper will mirror.
- `tests/test_trend_pass.py` - New test file covering all improvement areas.

### Notes

- Unit tests go in `tests/test_trend_pass.py` alongside the existing test files.
- Run tests with `pytest tests/test_trend_pass.py` (or `pytest tests/` for the full suite).
- No database migrations are needed: `pipeline_runs.metadata` is already `JSONB DEFAULT '{}'`.
- `run_trend_pass()` will return a new `TrendPassResult` dataclass instead of a bare `str`. Update all call sites.
- The `reports` table schema (`sql/001_init.sql:66-76`) confirms `report_type TEXT NOT NULL` and `title TEXT` columns exist. Values `'draft'` and `'final'` are used in `pipeline.py`.

### Pre-implementation verification

Before starting task 1.0, run this check:
```bash
grep -rn "run_trend_pass\|from.*trend_pass" src/ tests/ scripts/ --include="*.py"
```
This confirms all call sites that need updating for the return type change. As of the last audit, only `src/pipeline.py:26` (import) and `src/pipeline.py:595` (call) reference `run_trend_pass`. No existing tests cover it.

## Instructions for Completing Tasks

IMPORTANT: As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

---

## Batch 1 — Data structures, prompts, and contracts

> **Goal:** Define the dataclasses, prompt templates, and JSON parser that everything else depends on. Small, verifiable, no runtime dependencies.

- [x] 0.0 Create feature branch
  - [x] 0.1 Confirm the working branch is correct (`git branch --show-current`)
  - [x] 0.2 Run `grep -rn "run_trend_pass\|from.*trend_pass" src/ tests/ scripts/ --include="*.py"` to confirm all call sites for the return type change. Record the results here before proceeding.
    - Result recorded 2026-02-18: `src/pipeline.py:26`, `src/pipeline.py:595`, `src/generation/trend_pass.py:21` (note: command emitted `grep: scripts/: No such file or directory`).

- [x] 1.0 Define TrendCandidate, TrendPassResult, TrendPassError, and JSON parser
  - [x] 1.1 Define `TrendCandidate` dataclass in `trend_pass.py` with fields `rank: int`, `topic: str`, `justification: str`, `source_count: int`.
  - [x] 1.2 Define `TrendPassResult` dataclass with fields `topic: str`, `candidates: list[TrendCandidate]`, `lookback_days: int`, `dedup_max_similarity: float | None`.
  - [x] 1.3 Define `TrendPassError(Exception)` in `trend_pass.py` with a `candidates_tried: list[dict]` field. Each dict contains the candidate topic, rejection reason (e.g. "validation_failed", "dedup_exact", "dedup_semantic", "reprompt_failed"), and the highest similarity score if applicable.
  - [x] 1.4 Write `_parse_trend_candidates(raw: str) -> list[TrendCandidate]` that calls `json.loads()`, validates the structure (list of dicts with `rank`, `topic`, `justification`, `source_count`), and returns an empty list on any parse or validation error. Must also return an empty list if fewer than 2 candidates are returned (PRD 4.1 requirement 5).

- [x] 2.0 Update prompt templates for ranked-candidate output with early-signal ranking
  - [x] 2.1 Replace `TREND_USER_TEMPLATE` in `prompts.py` to request a JSON array of 3–5 objects, each with keys `rank` (int), `topic` (10–20 word phrase), `justification` (≤ 25 words), and `source_count` (int). Include a `{recent_topics_block}` placeholder so historical topics can be injected. Include a `{source_activity_summary}` placeholder for the time-bucket counts.
  - [x] 2.2 Add a `## Ranking criteria` section to the template that instructs the model to weight candidates by: (a) velocity — mentions accelerating in the last 2 days vs. prior 5 days, (b) cross-source convergence — appearing in both `[ARTICLE]` and `[TRANSCRIPT]` sources, (c) first-appearance recency — earliest mention within the last 48 hours. Instruct the model to rank lower any topic discussed at a flat rate across the entire window.
  - [x] 2.3 Add `TREND_REPROMPT_USER_TEMPLATE` — a short follow-up prompt that shows the rejected phrase and asks for a more specific 10–20 word replacement, returning plain text (not JSON). Note: re-prompt responses skip `_parse_trend_candidates()` and go directly through `_validate_topic()`.
  - [x] 2.4 Update `build_trend_prompt(*, sources_summary, recent_topics_block, source_activity_summary)` to accept and format all three placeholders.
  - [x] 2.5 Add `build_trend_reprompt(*, rejected_phrase)` that formats `TREND_REPROMPT_USER_TEMPLATE`.

---

## Batch 2 — Source building and adaptive window

> **Goal:** Refactor the SQL query and source formatting. Self-contained, no interaction with LLM call or validation logic. Can be tested independently.

- [x] 3.0 Refactor source-list building (snippet sizing, labels, relative age, activity summary)
  - [x] 3.1 Update the SQL query in `run_trend_pass()` to also `SELECT source_type, created_at` alongside `title`, `metadata ->> 'content'`, and `published_at`.
  - [x] 3.2 Add module-level constants: `_LONG_SNIPPET_CHARS = 800`, `_LONG_SNIPPET_TOP_N = 10`, keep existing `_CONTENT_SNIPPET_CHARS = 300`.
  - [x] 3.3 Write `_relative_age(dt: datetime, now: datetime) -> str` that returns a human-readable string like `"2 days ago"` or `"today"`.
  - [x] 3.4 Write `_source_type_label(source_type: str) -> str` that returns `"[TRANSCRIPT]"` for `source_type == "youtube"` and `"[ARTICLE]"` for everything else.
  - [x] 3.5 Write `_build_sources_summary(rows: list, now: datetime) -> str` that: sorts rows by `(published_at or created_at) DESC`, assigns 800-char snippets to the top 10 and 300-char snippets to the rest, and formats each line as `[LABEL | N days ago] Title: <snippet>`.
  - [x] 3.6 Write `_build_source_activity_summary(rows: list, now: datetime, lookback_days: int) -> str` that groups rows into two time buckets ("last 2 days" and "3–N days ago"), counts articles vs. transcripts in each bucket, and returns the formatted summary block (see PRD Section 4.8 / 6 for format).
  - [x] 3.7 Replace the inline source-formatting loop in `run_trend_pass()` with calls to `_build_sources_summary()` and `_build_source_activity_summary()`.

- [x] 4.0 Implement adaptive lookback window
  - [x] 4.1 Extract the source-querying SQL into `_query_sources(connection, lookback_days: int) -> list` so it can be called with different window sizes.
  - [x] 4.2 Add constant `_MIN_SOURCES_THRESHOLD = 10` in `trend_pass.py`.
  - [x] 4.3 In `run_trend_pass()`, after calling `_query_sources(connection, lookback_days)`, check `len(rows) < _MIN_SOURCES_THRESHOLD`; if so, re-query with `lookback_days * 2` (i.e., 14 days when default is 7) and log an info message.
  - [x] 4.4 Track the actual `lookback_days` used (7 or 14) and include it in the returned `TrendPassResult.lookback_days` field.

---

## Batch 3 — Core logic: dedup, validation, and LLM call wiring

> **Goal:** The hardest batch — the candidate selection loop, two-tier dedup, validation, re-prompt flow. These functions call each other, so they must be built together.

- [ ] 5.0 Wire up the LLM call and return type
  - [ ] 5.1 Raise `max_tokens` in the trend LLM call from `100` to `600` to accommodate 3–5 candidates with justifications.
  - [ ] 5.2 Update `run_trend_pass()` to return `TrendPassResult` instead of `str`. On fallback (`_FALLBACK_TOPIC`), return `TrendPassResult(topic=_FALLBACK_TOPIC, candidates=[], lookback_days=<actual>, dedup_max_similarity=None)`. Fallback paths must return *before* the validation/dedup loop — `_FALLBACK_TOPIC` must never pass through `_validate_topic()`.

- [ ] 6.0 Implement historical topic deduplication (two-tier)
  - [ ] 6.1 Add constant `_DEDUP_SIMILARITY_THRESHOLD = 0.85` in `trend_pass.py`.
  - [ ] 6.2 Write `_fetch_recent_report_topics(connection, limit: int = 10) -> list[str]` that queries `reports` where `report_type = 'final'` ordered by `created_at DESC LIMIT <limit>` and returns the `title` column values (skipping NULLs).
  - [ ] 6.3 Write `_normalise_text(text: str) -> str` that lowercases, strips punctuation (using `str.translate` or regex), and collapses whitespace. This is used for Tier 1 deduplication.
  - [ ] 6.4 Write `_is_exact_duplicate(candidate: str, historical_topics: list[str]) -> bool` that normalises both sides and checks for exact equality against any historical topic. This is Tier 1 — zero cost.
  - [ ] 6.5 Write `_embed_texts(texts: list[str], settings: Settings) -> list[list[float]]` that creates an OpenAI client and calls the embeddings API for a **single batch** of raw strings (no chunk IDs), with the same retry logic as `_embed_batch()` in `embeddings.py`.
  - [ ] 6.6 Write `_cosine_similarity(a: list[float], b: list[float]) -> float` using dot product (valid because OpenAI embeddings are unit-normalised).
  - [ ] 6.7 Write `_is_semantic_duplicate(candidate_topic: str, historical_topics: list[str], settings: Settings) -> tuple[bool, float]` that embeds the candidate and all historical topics in one batch call, computes pairwise cosine similarity, and returns `(True, max_score)` if any similarity ≥ `_DEDUP_SIMILARITY_THRESHOLD`, or `(False, max_score)` otherwise. The max score is always returned for logging/calibration.
  - [ ] 6.8 Write `_is_duplicate(candidate_topic: str, historical_topics: list[str], settings: Settings) -> tuple[bool, float | None]` that runs Tier 1 first (returns `(True, None)` on exact match), then Tier 2 only if Tier 1 passes (returns the Tier 2 result). This avoids embedding API calls for obvious repeats.
  - [ ] 6.9 Build the `recent_topics_block` string (for prompt injection per task 2.1) from the fetched historical topics and pass it into `build_trend_prompt()`.
  - [ ] 6.10 In the candidate selection loop, call `_is_duplicate()` for each candidate in rank order; skip duplicates. Log the highest similarity score for every candidate checked. If all candidates are duplicates, log a warning and fall back to candidate #1.
  - [ ] 6.11 Store the highest similarity score across all checked candidates in `TrendPassResult.dedup_max_similarity` and write it to `pipeline_runs.metadata['dedup_max_similarity']` for threshold calibration.

- [ ] 7.0 Implement output validation, re-prompt logic
  - [ ] 7.1 Define `_KNOWN_BAD_PATTERNS: frozenset[str]` containing `"football analysis"` and `"tactical trends"`. Note: `"emerging football tactical trends"` is deliberately excluded — it is `_FALLBACK_TOPIC`, not a validation pattern. See PRD Section 6 "Fallback topic".
  - [ ] 7.2 Write `_validate_topic(topic: str) -> bool` that returns `True` only if: word count is in range 5–25 and the lowercased topic is not in `_KNOWN_BAD_PATTERNS`.
  - [ ] 7.3 Write `_reprompt_for_topic(rejected_phrase: str, settings: Settings, client: Anthropic) -> str` that calls the LLM once using `build_trend_reprompt()`, parses the plain-text response (not JSON — no `_parse_trend_candidates()` step), and returns the stripped result (or empty string on error).
  - [ ] 7.4 In the candidate selection loop: for each candidate `topic`, first run deduplication (task 6.10), then call `_validate_topic()`; if validation fails, call `_reprompt_for_topic()` once and re-validate; if still invalid, record the rejection reason and move to the next candidate. After exhausting all candidates, raise `TrendPassError` with the full `candidates_tried` list.

---

## Batch 4 — Pipeline integration and tests

> **Goal:** Wire `TrendPassResult` into `pipeline.py`, write all test cases. By now the implementation exists, so this batch is connecting and verifying.

- [ ] 8.0 Update pipeline.py to handle TrendPassResult and TrendPassError
  - [ ] 8.1 Import `TrendPassResult` and `TrendPassError` from `src.generation.trend_pass` in `pipeline.py`.
  - [ ] 8.2 Update the `run_trend_pass()` call site (line 595) to capture a `TrendPassResult`; extract `.topic` for downstream use.
  - [ ] 8.3 Wrap the `run_trend_pass()` call in a `try/except TrendPassError` block; on catch, log a warning (including `e.candidates_tried`) and set `topic = _FALLBACK_TOPIC` (import the constant or use the string directly).
  - [ ] 8.4 After the trend call, write `trend_candidates` (serialised candidate list), `trend_lookback_days`, and `dedup_max_similarity` into the existing `stage_metrics` dict so they are stored in `pipeline_runs.metadata` alongside the other generation metrics.

- [ ] 9.0 Write tests
  - [ ] 9.1 Test `_relative_age()`: same day → "today"; 1 day → "1 day ago"; 5 days → "5 days ago".
  - [ ] 9.2 Test `_build_sources_summary()`: top 10 rows get 800-char snippets; row 11 gets 300-char snippet; labels are `[ARTICLE]` or `[TRANSCRIPT]` correctly; rows are sorted by recency.
  - [ ] 9.3 Test `_build_source_activity_summary()`: 3 articles + 2 transcripts in last 2 days, 5 articles + 1 transcript in 3–7 days → correct formatted output. Test with expanded 14-day window → bucket label changes to "3–14 days ago".
  - [ ] 9.4 Test `_parse_trend_candidates()`: valid JSON with 3 candidates returns 3 `TrendCandidate` objects; malformed JSON returns empty list; missing keys return empty list; 1 candidate returns empty list (minimum 2 required).
  - [ ] 9.5 Test `_validate_topic()`: 4-word phrase → False; 26-word phrase → False; known-bad string → False; valid 10-word phrase → True. Confirm `"emerging football tactical trends"` (the fallback) is NOT in `_KNOWN_BAD_PATTERNS`.
  - [ ] 9.6 Test `_normalise_text()`: mixed case → lowercase; punctuation stripped; multiple spaces collapsed.
  - [ ] 9.7 Test `_is_exact_duplicate()`: exact string match → True; same meaning different wording → False; match after normalisation (case, punctuation) → True.
  - [ ] 9.8 Test `_cosine_similarity()`: identical unit vectors → 1.0; orthogonal vectors → 0.0.
  - [ ] 9.9 Test `_is_duplicate()` two-tier flow: exact match → returns `(True, None)` without calling `_embed_texts`; no exact match → calls `_embed_texts` and returns Tier 2 result. Mock `_embed_texts()` to verify it is only called when Tier 1 passes.
  - [ ] 9.10 Test deduplication scoring: mock `_embed_texts()` to return vectors with similarity ≥ 0.85 → candidate is skipped; similarity < 0.85 → candidate is accepted; all candidates duplicate → candidate #1 is returned with a warning. Verify `dedup_max_similarity` is recorded.
  - [ ] 9.11 Test adaptive window: mock `_query_sources()` to return 5 rows for 7 days and 15 rows for 14 days → function uses 14-day data and sets `TrendPassResult.lookback_days = 14`; zero rows for both → `_FALLBACK_TOPIC` returned with `dedup_max_similarity = None`.
  - [ ] 9.12 Test validation + re-prompt flow: first candidate invalid, re-prompt returns valid → accepted; re-prompt also invalid → next candidate tried; all candidates invalid → `TrendPassError` raised with `candidates_tried` list populated.
  - [ ] 9.13 Test `TrendPassError` carries data: raise with 3 candidates tried, catch it, verify `e.candidates_tried` has 3 entries with topic and reason fields.
  - [ ] 9.14 Test `run_generation()` in `pipeline.py`: mock `run_trend_pass()` to raise `TrendPassError` → function catches it, uses fallback topic, does not crash.
  - [ ] 9.15 Test fallback bypass: when `run_trend_pass` returns `_FALLBACK_TOPIC` (no sources found), verify `_validate_topic()` was never called. This confirms the fallback path bypasses validation.
