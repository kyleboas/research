# PRD: Enhanced Trend Selection

## 1. Introduction / Overview

The trend discovery pass (`run_trend_pass`) scans recently ingested football sources and asks an LLM to identify an emerging topic. That topic becomes the foundation for every downstream pipeline stage: retrieval queries, draft, critique, and revision.

The current implementation has structural weaknesses that produce low-quality, redundant, or late topics. This PRD describes targeted fixes for those weaknesses, adds early-signal detection so the system surfaces trends *before* they become mainstream, and includes any supporting database or schema changes required.

---

## 2. Goals

1. **Improve topic quality** — surface the single best emerging trend by forcing the model to reason comparatively across 3–5 ranked candidates before committing to one.
2. **Improve topic novelty** — prevent the same (or semantically very similar) topic from appearing in consecutive reports by checking against the last 10 published report titles.
3. **Improve source signal** — give the model richer content for the most recent sources (800 chars) while keeping older sources at 300 chars.
4. **Improve source context** — include relative age and source type labels ([ARTICLE] / [TRANSCRIPT]) so the model can weight recency and source quality naturally.
5. **Improve resilience** — expand the lookback window from 7 to 14 days when fewer than 10 sources are found before falling back; flag reports generated with the expanded window.
6. **Prevent poisoned downstream queries** — validate the chosen topic phrase against length and content rules; re-prompt once on failure; fall back through the ranked candidate list before aborting.
7. **Preserve candidates for analysis** — store all ranked candidates (not just the winner) in `pipeline_runs.metadata` for observability and future use.
8. **Detect trends early** — weight source velocity (how fast mentions are accelerating), cross-source convergence (a topic appearing across independent source types), and recency of first appearance to favour topics that are *emerging now* over those already saturated.

---

## 3. User Stories

- **As the pipeline**, I want to receive a high-quality, non-repetitive trend topic so that every downstream stage is built on a strong foundation.
- **As an operator reviewing pipeline runs**, I want to see all ranked trend candidates (not just the one chosen) so that I can understand why a particular topic was selected.
- **As an operator**, I want the pipeline to gracefully handle slow news weeks by expanding the lookback window rather than emitting a useless generic topic.
- **As a developer**, I want topic validation to catch vague or malformed LLM output early so that bad topics never reach retrieval or generation.
- **As a reader**, I want the report to cover a trend I haven't already seen everywhere, so the content feels ahead of the curve rather than behind it.

---

## 4. Functional Requirements

### 4.1 Ranked candidate generation

1. The system must request 3–5 ranked trend candidates from the LLM instead of a single topic phrase.
2. Each candidate must include a one-line justification (≤ 25 words).
3. Each candidate must include a `source_count` field — the number of distinct sources (by title) the LLM observed supporting that topic.
4. The system must auto-select candidate #1 (rank 1) as the working topic.
5. If the LLM returns fewer than 2 candidates, the response must be treated as a parse failure and trigger the re-prompt flow (Section 4.6).
6. The system must store all returned candidates (rank, topic phrase, justification, source_count) as a JSON array in `pipeline_runs.metadata['trend_candidates']`.

### 4.2 Source snippet sizing

7. The system must sort sources by `published_at DESC, created_at DESC` (ingestion timestamp as tiebreaker) before slicing.
8. The system must assign an 800-character snippet to the top 10 most recent sources.
9. The system must assign a 300-character snippet to all remaining sources.
10. The maximum number of sources included in the prompt must remain 60.

### 4.3 Source context labels

11. The system must include each source's relative age in the prompt (e.g., "2 days ago", "5 days ago"), computed from `COALESCE(published_at, created_at)` relative to pipeline execution time.
12. The system must label each source with its type: sources with `source_type = 'youtube'` must be labelled `[TRANSCRIPT]`; all others must be labelled `[ARTICLE]`.
13. The relative age and type label must appear on the same line as the source title, before the snippet.

### 4.4 Historical topic deduplication

Deduplication uses a two-tier strategy to avoid unnecessary embedding API calls on every run:

14. **Tier 1 — exact / normalised string match (always runs, zero cost).** The system must normalise both the candidate and each historical topic (lowercase, strip punctuation, collapse whitespace) and check for exact equality. If a normalised match is found the candidate is immediately marked as a duplicate.
15. **Tier 2 — semantic similarity via embeddings (runs only if Tier 1 passes).** The system must query the `title` column of the 10 most recent rows in the `reports` table (ordered by `created_at DESC`) where `report_type = 'final'`.
16. The system must embed the candidate and all historical topics in a **single batch** call using the existing OpenAI embedding infrastructure. (OpenAI's embeddings endpoint accepts a list of inputs; this is one API call, not N+1.)
17. The system must compute cosine similarity between the candidate embedding and each historical topic embedding via dot product (OpenAI `text-embedding-3-*` embeddings are unit-normalised).
18. If any historical topic has cosine similarity ≥ `_DEDUP_SIMILARITY_THRESHOLD` with the candidate, the system must skip that candidate and try the next ranked candidate.
19. The system must log the highest similarity score observed for every candidate checked, regardless of whether it crossed the threshold. This data is essential for calibrating the threshold over the first 20+ runs.
20. If all candidates are eliminated by deduplication, the system must proceed with candidate #1 regardless (topic repetition is preferable to an abort at this stage) and log a warning.

### 4.5 Adaptive lookback window

21. After querying sources with the default 7-day window, the system must count the number of rows returned.
22. If fewer than 10 sources are found, the system must re-query using a 14-day window before continuing.
23. When the 14-day window is used, the system must set `pipeline_runs.metadata['trend_lookback_days'] = 14` (versus the default value of `7`).
24. If even the 14-day query returns zero sources, the system must fall back to `_FALLBACK_TOPIC` and log a warning.

### 4.6 Output validation and re-prompting

25. After selecting a topic (the deduplication-passing candidate), the system must validate it against these rules:
    - Word count is between 5 and 25 (inclusive).
    - The string does not match any of the known-bad patterns: `"football analysis"`, `"tactical trends"`, or any string ≤ 3 words.
26. **`_FALLBACK_TOPIC` must be exempt from validation.** Because the fallback string is intentionally generic, it must bypass `_validate_topic()`. Validation only applies to LLM-generated candidate topics. To make this explicit, the fallback path must return its result *before* the validation loop is entered.
27. If validation fails, the system must re-prompt the LLM once with the same prompt but with an explicit instruction to return a more specific phrase. The re-prompt returns plain text (not JSON) — there is no `_parse_trend_candidates()` step for the re-prompted response; it goes directly through `_validate_topic()`.
28. If the re-prompted response also fails validation, the system must attempt the next ranked candidate in order.
29. If no candidate passes validation (after re-prompting the last one), the system must raise a `TrendPassError` exception (a new exception class defined in `trend_pass.py`) so the caller can decide whether to abort or fall back. `TrendPassError` must carry the list of attempted candidates and their rejection reasons as a `candidates_tried: list[dict]` field for downstream logging.
30. `run_trend_pass` callers in `pipeline.py` must catch `TrendPassError`, log it (including the attached candidates list), and fall back to `_FALLBACK_TOPIC` so the pipeline does not crash.

### 4.7 Velocity and early-signal scoring

The prompt must instruct the LLM to weight candidates using three early-signal heuristics. These are encoded in the prompt, not in code — the LLM applies them during ranking:

31. **Velocity** — topics where the ratio of mentions in the last 2 days to mentions in the prior 5 days is highest should rank above topics with a flat mention curve. The prompt must explicitly tell the model to look for acceleration, not just volume.
32. **Cross-source convergence** — topics appearing in both `[ARTICLE]` and `[TRANSCRIPT]` sources (i.e., written analysis *and* video/podcast discussion) should rank above topics confined to a single source type. Convergence across independent channels is a leading indicator.
33. **First-appearance recency** — topics whose earliest mention in the source window is within the last 48 hours should rank above topics that have been present for the entire 7-day window. A topic appearing for the first time yesterday is more likely to be pre-mainstream than one that has been discussed all week.
34. The prompt must include a `## Ranking criteria` section that makes these three heuristics explicit and instructs the model to apply them when ordering candidates.

### 4.8 Source mention counts in prompt

35. To give the LLM the raw data it needs for velocity scoring (Section 4.7), the system must compute and include per-source mention counts by time bucket.
36. After building the source list, the system must group sources into two buckets: "last 2 days" and "3–7 days ago" (or "3–14 days ago" if the expanded window is in use).
37. The prompt must include a short summary block before the source list, e.g.:
    ```
    ## Source activity summary
    Sources last 2 days: 18 (12 articles, 6 transcripts)
    Sources 3–7 days ago: 34 (28 articles, 6 transcripts)
    ```
    This gives the model an explicit signal about where the volume is concentrated.

---

## 5. Non-Goals (Out of Scope)

- Manual or human-in-the-loop candidate selection (always auto-select #1).
- Changing the downstream retrieval, draft, critique, or revision passes.
- Storing candidate embeddings persistently in the `embeddings` table.
- Modifying the `reports` schema — topic deduplication queries existing `title` and `metadata` columns only.
- Adding a UI or API for reviewing trend candidates.
- Changing the frequency or scheduling of pipeline runs.
- Building a persistent topic-tracking database across runs (future work — see Section 10).

---

## 6. Design Considerations

### Prompt format for ranked candidates

The updated `TREND_USER_TEMPLATE` must request JSON output in the following shape so it can be parsed deterministically:

```json
[
  {
    "rank": 1,
    "topic": "<10–20 word phrase>",
    "justification": "<one sentence, ≤ 25 words>",
    "source_count": 5
  },
  ...
]
```

A minimum of **2 candidates** is required for the response to be considered valid. If fewer are returned, the system must treat it as a parse failure and trigger the re-prompt flow.

The `max_tokens` budget for the trend LLM call must be raised to accommodate 3–5 candidates with justifications. A value of **600 tokens** is recommended.

### Source list format

Each line in the sources summary must follow this format:

```
[ARTICLE | 3 days ago] Title: <snippet up to 800 or 300 chars>
```

### Source activity summary format

Before the source list, include a summary block:

```
## Source activity summary
Sources last 2 days: 18 (12 articles, 6 transcripts)
Sources 3–7 days ago: 34 (28 articles, 6 transcripts)
```

### Ranking criteria block in prompt

The prompt must include a section like:

```
## Ranking criteria
Rank candidates higher when they show:
1. VELOCITY — more mentions in the last 2 days relative to the prior 5 days (accelerating, not just popular)
2. CONVERGENCE — discussed across both articles and transcripts (independent sources arriving at the same idea)
3. RECENCY OF FIRST APPEARANCE — the topic's earliest mention in the source window is within the last 48 hours (newly surfacing, not a week-long discussion)
Rank candidates lower when they are already widely discussed across the entire window with no acceleration.
```

### Two-tier deduplication

Deduplication runs in two tiers to avoid embedding API overhead on every run:

1. **Tier 1: normalised string match** — lowercase, strip punctuation, collapse whitespace, compare for exact equality. This catches verbatim and near-verbatim repeats at zero cost.
2. **Tier 2: semantic similarity** — only if Tier 1 passes. Embeds all texts in a single batch API call (not N+1 individual calls).

This means most runs where the LLM produces a genuinely new topic will incur zero embedding cost at the deduplication step.

### Semantic similarity threshold

A cosine similarity threshold of **0.85** is used for Tier 2 deduplication (requirement 18). This value should be defined as a named constant `_DEDUP_SIMILARITY_THRESHOLD = 0.85` in `trend_pass.py`.

**Calibration plan:** for the first 20 pipeline runs after deployment, every candidate's highest similarity score must be logged to `pipeline_runs.metadata['dedup_max_similarity']` (requirement 19). After 20 runs, review the distribution and adjust the threshold if needed. If most non-duplicate topics score < 0.70 and true duplicates score > 0.90, the threshold is well-placed. If the gap is narrow, lower the threshold to 0.80.

### Top-N snippet count

The number of sources that receive 800-char snippets should be a named constant `_LONG_SNIPPET_TOP_N = 10` in `trend_pass.py`.

### Fallback topic

`_FALLBACK_TOPIC` is `"emerging football tactical trends"`. This string is intentionally generic and would fail validation (it is itself a known-bad pattern). Therefore:

- `_FALLBACK_TOPIC` must **never** pass through `_validate_topic()`.
- All fallback paths (no sources found, all candidates exhausted, `TrendPassError` caught in pipeline) must return the fallback *directly*, bypassing the validation/dedup loop.
- `_FALLBACK_TOPIC` must be removed from the `_KNOWN_BAD_PATTERNS` set to avoid confusion, since it is a distinct concept: it is the safe fallback, not a pattern to reject. The known-bad list should contain `"football analysis"` and `"tactical trends"` only.

---

## 7. Technical Considerations

- **Embedding calls** — Tier 2 deduplication embeds the candidate + all historical topics in a **single batch API call** using the OpenAI embeddings endpoint (which accepts a list of inputs). With 10 historical titles, this is 1 API call embedding 11 strings — not 11 separate calls. The existing `embed_chunks` infrastructure handles batching; a lightweight wrapper (`_embed_texts`) that accepts raw strings (not `Chunk` objects) is needed.
- **Tier 1 string match** — zero-cost normalisation (lowercase + strip punctuation + collapse whitespace). Runs before any embedding call. Prevents unnecessary API usage on obvious repeats.
- **JSON parsing** — the new LLM response is JSON. Use `json.loads()` with a `try/except` around parsing; if parsing fails or fewer than 2 candidates are returned, treat it as a validation failure and trigger the re-prompt flow.
- **`pipeline_runs.metadata`** — this column is already `JSONB DEFAULT '{}'`, so storing `trend_candidates`, `trend_lookback_days`, and `dedup_max_similarity` requires no schema migration.
- **Cosine similarity** — compute via dot product. OpenAI `text-embedding-3-*` embeddings are unit-normalised, so dot product == cosine similarity.
- **`TrendPassError`** — should subclass `Exception`; must include a `candidates_tried: list[dict]` field containing each candidate's topic, rejection reason (validation failure, dedup hit, re-prompt failure), and the highest similarity score if applicable. This costs nothing and is essential for debugging.
- **Return type change** — `run_trend_pass()` changes from returning `str` to `TrendPassResult`. This is a breaking change. All call sites must be updated — currently only `src/pipeline.py:595`, but an implementation-time grep for `run_trend_pass` across the entire repo must confirm no other callers exist (tests, scripts, notebooks).
- **Re-prompt response format** — the re-prompt returns plain text, not JSON. There is no `_parse_trend_candidates()` step for re-prompted responses; the stripped text goes directly through `_validate_topic()`.
- **Source activity summary** — computed in `_build_sources_summary()` by partitioning rows into time buckets. This is a pure Python computation over the already-fetched rows; no additional DB query is needed.

---

## 8. Success Metrics

| Metric | Target |
|---|---|
| Topic word count always in range 5–25 | 100 % of runs |
| Topic deduplication triggered at least once in first 20 runs | ≥ 1 occurrence |
| `trend_candidates` key present in `pipeline_runs.metadata` | 100 % of trend-pass runs |
| `trend_lookback_days` key present in `pipeline_runs.metadata` | 100 % of trend-pass runs |
| `dedup_max_similarity` key present in `pipeline_runs.metadata` | 100 % of trend-pass runs where dedup Tier 2 ran |
| Pipeline never crashes due to a bad trend topic (TrendPassError caught) | 100 % of runs |
| Runs using 14-day window flagged in metadata | 100 % of such runs |
| Average `source_count` of selected candidate ≥ 3 | Over first 20 runs |
| Selected topic's earliest source mention is ≤ 3 days old | ≥ 60 % of runs |

---

## 9. Resolved Questions

The following were open questions in the original draft. They are now resolved:

1. **`_LONG_SNIPPET_TOP_N` configurability** — left as a module-level constant. Over-configuring early adds complexity with no benefit.
2. **Upfront vs. lazy deduplication** — lazy (check candidate #1 first, fall through on failure). Upfront filtering wastes embedding calls on candidates that may never be needed.
3. **0.85 cosine similarity threshold** — ship with 0.85 but log all scores (requirement 19) for empirical calibration after 20 runs. See calibration plan in Section 6.
4. **`TrendPassError` carrying failed candidates** — yes. The `candidates_tried` field is required (requirement 29).

---

## 10. Future Work

These items are explicitly out of scope for this PRD but are natural follow-ons:

- **Persistent topic tracker** — store every candidate (not just the winner) with its embedding in a dedicated `trend_topics` table. Over time, this builds a map of topic trajectories — which topics were detected early and later became mainstream, which were false positives, etc. This data can train a classifier or fine-tune the prompt.
- **Automated threshold tuning** — after accumulating 50+ runs of `dedup_max_similarity` data, compute the optimal threshold automatically rather than relying on manual review.
- **Source authority weighting** — not all sources are equal. A well-known analyst flagging a topic is a stronger signal than a generic aggregator. Future work could assign authority scores to sources and surface them in the prompt.
- **Multi-run momentum tracking** — compare the current run's candidates against previous runs' candidates. A topic that appears as candidate #3 in run N, candidate #2 in run N+1, and candidate #1 in run N+2 is showing sustained momentum even if it never made it to the report before.
