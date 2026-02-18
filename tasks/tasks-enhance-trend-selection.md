# Tasks: Enhanced Trend Selection

## Relevant Files

- `src/generation/trend_pass.py` - Primary file for all six improvements: snippet sizing, source labels, ranked candidates, deduplication, adaptive window, validation, and TrendPassError.
- `src/generation/prompts.py` - `TREND_SYSTEM`, `TREND_USER_TEMPLATE`, `TREND_REPROMPT_USER_TEMPLATE`, and `build_trend_prompt()` / `build_trend_reprompt()` must be updated.
- `src/pipeline.py` - Caller of `run_trend_pass()`; must handle the new `TrendPassResult` return type, catch `TrendPassError`, and write `trend_candidates` / `trend_lookback_days` into `pipeline_runs.metadata`.
- `src/processing/embeddings.py` - Reference only. Contains `_embed_batch()` and the OpenAI client setup that the new `_embed_texts()` helper will mirror.
- `tests/test_trend_pass.py` - New test file covering all six improvement areas.

### Notes

- Unit tests go in `tests/test_trend_pass.py` alongside the existing test files.
- Run tests with `pytest tests/test_trend_pass.py` (or `pytest tests/` for the full suite).
- No database migrations are needed: `pipeline_runs.metadata` is already `JSONB DEFAULT '{}'`.
- `run_trend_pass()` will return a new `TrendPassResult` dataclass instead of a bare `str`. Update all call sites.

## Instructions for Completing Tasks

IMPORTANT: As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [ ] 0.0 Create feature branch
  - [ ] 0.1 Confirm the working branch is `claude/enhance-trend-selection-aUWck` (`git branch --show-current`)

- [ ] 1.0 Update prompt templates for ranked-candidate output
  - [ ] 1.1 Replace `TREND_USER_TEMPLATE` in `prompts.py` to request a JSON array of 3–5 objects, each with keys `rank` (int), `topic` (10–20 word phrase), and `justification` (≤ 25 words). Include a `{recent_topics_block}` placeholder so historical topics can be injected.
  - [ ] 1.2 Add `TREND_REPROMPT_USER_TEMPLATE` — a short follow-up prompt that shows the rejected phrase and asks for a more specific 10–20 word replacement, returning plain text (not JSON).
  - [ ] 1.3 Update `build_trend_prompt(*, sources_summary, recent_topics_block)` to accept and format both placeholders.
  - [ ] 1.4 Add `build_trend_reprompt(*, rejected_phrase)` that formats `TREND_REPROMPT_USER_TEMPLATE`.

- [ ] 2.0 Refactor source-list building (snippet sizing, labels, relative age)
  - [ ] 2.1 Update the SQL query in `run_trend_pass()` to also `SELECT source_type, created_at` alongside `title`, `metadata ->> 'content'`, and `published_at`.
  - [ ] 2.2 Add module-level constants: `_LONG_SNIPPET_CHARS = 800`, `_LONG_SNIPPET_TOP_N = 10`, keep existing `_CONTENT_SNIPPET_CHARS = 300`.
  - [ ] 2.3 Write `_relative_age(dt: datetime, now: datetime) -> str` that returns a human-readable string like `"2 days ago"` or `"today"`.
  - [ ] 2.4 Write `_source_type_label(source_type: str) -> str` that returns `"[TRANSCRIPT]"` for `source_type == "youtube"` and `"[ARTICLE]"` for everything else.
  - [ ] 2.5 Write `_build_sources_summary(rows: list, now: datetime) -> str` that: sorts rows by `(published_at or created_at) DESC`, assigns 800-char snippets to the top 10 and 300-char snippets to the rest, and formats each line as `[LABEL | N days ago] Title: <snippet>`.
  - [ ] 2.6 Replace the inline source-formatting loop in `run_trend_pass()` with a call to `_build_sources_summary()`.

- [ ] 3.0 Implement ranked-candidate LLM call, JSON parsing, and TrendPassResult
  - [ ] 3.1 Define `TrendCandidate` dataclass in `trend_pass.py` with fields `rank: int`, `topic: str`, `justification: str`.
  - [ ] 3.2 Define `TrendPassResult` dataclass with fields `topic: str`, `candidates: list[TrendCandidate]`, `lookback_days: int`. This replaces the bare `str` return type of `run_trend_pass()`.
  - [ ] 3.3 Write `_parse_trend_candidates(raw: str) -> list[TrendCandidate]` that calls `json.loads()`, validates the structure (list of dicts with `rank`, `topic`, `justification`), and returns an empty list on any parse or validation error.
  - [ ] 3.4 Raise `max_tokens` in the trend LLM call from `100` to `600` to accommodate 3–5 candidates with justifications.
  - [ ] 3.5 Update `run_trend_pass()` to return `TrendPassResult` instead of `str`. On fallback (`_FALLBACK_TOPIC`), return `TrendPassResult(topic=_FALLBACK_TOPIC, candidates=[], lookback_days=<actual>)`.

- [ ] 4.0 Implement historical topic deduplication via semantic similarity
  - [ ] 4.1 Add constant `_DEDUP_SIMILARITY_THRESHOLD = 0.85` in `trend_pass.py`.
  - [ ] 4.2 Write `_fetch_recent_report_topics(connection, limit: int = 10) -> list[str]` that queries `reports` where `report_type = 'final'` ordered by `created_at DESC LIMIT <limit>` and returns the `title` column values (skipping NULLs).
  - [ ] 4.3 Write `_embed_texts(texts: list[str], settings: Settings) -> list[list[float]]` that creates an OpenAI client and calls the embeddings API for a batch of raw strings (no chunk IDs), with the same retry logic as `_embed_batch()` in `embeddings.py`.
  - [ ] 4.4 Write `_cosine_similarity(a: list[float], b: list[float]) -> float` using dot product (valid because OpenAI embeddings are unit-normalised).
  - [ ] 4.5 Write `_is_duplicate(candidate_topic: str, historical_topics: list[str], settings: Settings) -> bool` that embeds the candidate and all historical topics in one batch call, computes pairwise cosine similarity, and returns `True` if any similarity ≥ `_DEDUP_SIMILARITY_THRESHOLD`.
  - [ ] 4.6 Build the `recent_topics_block` string (for prompt injection per task 1.1) from the fetched historical topics and pass it into `build_trend_prompt()`.
  - [ ] 4.7 In the candidate selection loop, call `_is_duplicate()` for each candidate in rank order; skip duplicates. If all candidates are duplicates, log a warning and fall back to candidate #1.

- [ ] 5.0 Implement adaptive lookback window and metadata flagging
  - [ ] 5.1 Extract the source-querying SQL into `_query_sources(connection, lookback_days: int) -> list` so it can be called with different window sizes.
  - [ ] 5.2 Add constant `_MIN_SOURCES_THRESHOLD = 10` in `trend_pass.py`.
  - [ ] 5.3 In `run_trend_pass()`, after calling `_query_sources(connection, lookback_days)`, check `len(rows) < _MIN_SOURCES_THRESHOLD`; if so, re-query with `lookback_days * 2` (i.e., 14 days when default is 7) and log an info message.
  - [ ] 5.4 Track the actual `lookback_days` used (7 or 14) and include it in the returned `TrendPassResult.lookback_days` field.

- [ ] 6.0 Implement output validation, re-prompt logic, and TrendPassError
  - [ ] 6.1 Define `TrendPassError(Exception)` in `trend_pass.py` with a message describing which candidates were tried.
  - [ ] 6.2 Define `_KNOWN_BAD_PATTERNS: frozenset[str]` containing `"football analysis"`, `"tactical trends"`, `"emerging football tactical trends"`.
  - [ ] 6.3 Write `_validate_topic(topic: str) -> bool` that returns `True` only if: word count is in range 5–25 and the lowercased topic is not in `_KNOWN_BAD_PATTERNS`.
  - [ ] 6.4 Write `_reprompt_for_topic(rejected_phrase: str, settings: Settings, client: Anthropic) -> str` that calls the LLM once using `build_trend_reprompt()`, parses the plain-text response, and returns the stripped result (or empty string on error).
  - [ ] 6.5 In the candidate selection loop: for each candidate `topic`, call `_validate_topic()`; if it fails, call `_reprompt_for_topic()` once and re-validate; if still invalid, move to the next candidate. After exhausting all candidates, raise `TrendPassError`.

- [ ] 7.0 Update pipeline.py to handle TrendPassResult and TrendPassError
  - [ ] 7.1 Import `TrendPassResult` and `TrendPassError` from `src.generation.trend_pass` in `pipeline.py`.
  - [ ] 7.2 Update the `run_trend_pass()` call site (line 595) to capture a `TrendPassResult`; extract `.topic` for downstream use.
  - [ ] 7.3 Wrap the `run_trend_pass()` call in a `try/except TrendPassError` block; on catch, log a warning and set `topic = _FALLBACK_TOPIC` (import the constant or use the string directly).
  - [ ] 7.4 After the trend call, write `trend_candidates` (serialised candidate list) and `trend_lookback_days` into the existing `stage_metrics` dict so they are stored in `pipeline_runs.metadata` alongside the other generation metrics.

- [ ] 8.0 Write tests
  - [ ] 8.1 Test `_relative_age()`: same day → "today"; 1 day → "1 day ago"; 5 days → "5 days ago".
  - [ ] 8.2 Test `_build_sources_summary()`: top 10 rows get 800-char snippets; row 11 gets 300-char snippet; labels are `[ARTICLE]` or `[TRANSCRIPT]` correctly; rows are sorted by recency.
  - [ ] 8.3 Test `_parse_trend_candidates()`: valid JSON with 3 candidates returns 3 `TrendCandidate` objects; malformed JSON returns empty list; missing keys return empty list.
  - [ ] 8.4 Test `_validate_topic()`: 4-word phrase → False; 26-word phrase → False; known-bad string → False; valid 10-word phrase → True.
  - [ ] 8.5 Test `_cosine_similarity()`: identical unit vectors → 1.0; orthogonal vectors → 0.0.
  - [ ] 8.6 Test deduplication logic: mock `_embed_texts()` to return vectors with similarity ≥ 0.85 → candidate is skipped; similarity < 0.85 → candidate is accepted; all candidates duplicate → candidate #1 is returned with a warning.
  - [ ] 8.7 Test adaptive window: mock `_query_sources()` to return 5 rows for 7 days and 15 rows for 14 days → function uses 14-day data and sets `TrendPassResult.lookback_days = 14`; zero rows for both → `_FALLBACK_TOPIC` returned.
  - [ ] 8.8 Test validation + re-prompt flow: first candidate invalid, re-prompt returns valid → accepted; re-prompt also invalid → next candidate tried; all candidates invalid → `TrendPassError` raised.
  - [ ] 8.9 Test `run_generation()` in `pipeline.py`: mock `run_trend_pass()` to raise `TrendPassError` → function catches it, uses fallback topic, does not crash.
