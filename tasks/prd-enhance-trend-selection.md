# PRD: Enhanced Trend Selection

## 1. Introduction / Overview

The trend discovery pass (`run_trend_pass`) scans recently ingested football sources and asks an LLM to identify an emerging topic. That topic becomes the foundation for every downstream pipeline stage: retrieval queries, draft, critique, and revision.

The current implementation has six structural weaknesses that produce low-quality or redundant topics. This PRD describes targeted fixes for all six, as well as any supporting database or schema changes required.

---

## 2. Goals

1. **Improve topic quality** — surface the single best emerging trend by forcing the model to reason comparatively across 3–5 ranked candidates before committing to one.
2. **Improve topic novelty** — prevent the same (or semantically very similar) topic from appearing in consecutive reports by checking against the last 10 published report titles.
3. **Improve source signal** — give the model richer content for the most recent sources (800 chars) while keeping older sources at 300 chars.
4. **Improve source context** — include relative age and source type labels ([ARTICLE] / [TRANSCRIPT]) so the model can weight recency and source quality naturally.
5. **Improve resilience** — expand the lookback window from 7 to 14 days when fewer than 10 sources are found before falling back; flag reports generated with the expanded window.
6. **Prevent poisoned downstream queries** — validate the chosen topic phrase against length and content rules; re-prompt once on failure; fall back through the ranked candidate list before aborting.
7. **Preserve candidates for analysis** — store all ranked candidates (not just the winner) in `pipeline_runs.metadata` for observability and future use.

---

## 3. User Stories

- **As the pipeline**, I want to receive a high-quality, non-repetitive trend topic so that every downstream stage is built on a strong foundation.
- **As an operator reviewing pipeline runs**, I want to see all ranked trend candidates (not just the one chosen) so that I can understand why a particular topic was selected.
- **As an operator**, I want the pipeline to gracefully handle slow news weeks by expanding the lookback window rather than emitting a useless generic topic.
- **As a developer**, I want topic validation to catch vague or malformed LLM output early so that bad topics never reach retrieval or generation.

---

## 4. Functional Requirements

### 4.1 Ranked candidate generation

1. The system must request 3–5 ranked trend candidates from the LLM instead of a single topic phrase.
2. Each candidate must include a one-line justification (≤ 25 words).
3. The system must auto-select candidate #1 (rank 1) as the working topic.
4. The system must store all returned candidates (rank, topic phrase, justification) as a JSON array in `pipeline_runs.metadata['trend_candidates']`.

### 4.2 Source snippet sizing

5. The system must sort sources by `published_at DESC, created_at DESC` (ingestion timestamp as tiebreaker) before slicing.
6. The system must assign an 800-character snippet to the top 10 most recent sources.
7. The system must assign a 300-character snippet to all remaining sources.
8. The maximum number of sources included in the prompt must remain 60.

### 4.3 Source context labels

9. The system must include each source's relative age in the prompt (e.g., "2 days ago", "5 days ago"), computed from `COALESCE(published_at, created_at)` relative to pipeline execution time.
10. The system must label each source with its type: sources with `source_type = 'youtube'` must be labelled `[TRANSCRIPT]`; all others must be labelled `[ARTICLE]`.
11. The relative age and type label must appear on the same line as the source title, before the snippet.

### 4.4 Historical topic deduplication

12. The system must query the `title` column of the 10 most recent rows in the `reports` table (ordered by `created_at DESC`) where `report_type = 'final'`.
13. The system must embed the working topic candidate and each historical topic using the existing OpenAI embedding infrastructure.
14. The system must compute cosine similarity between the working candidate and each historical topic.
15. If any historical topic has cosine similarity ≥ 0.85 with the working candidate, the system must skip that candidate and try the next ranked candidate.
16. If all candidates are eliminated by deduplication, the system must proceed with candidate #1 regardless (topic repetition is preferable to an abort at this stage) and log a warning.

### 4.5 Adaptive lookback window

17. After querying sources with the default 7-day window, the system must count the number of rows returned.
18. If fewer than 10 sources are found, the system must re-query using a 14-day window before continuing.
19. When the 14-day window is used, the system must set `pipeline_runs.metadata['trend_lookback_days'] = 14` (versus the default value of `7`).
20. If even the 14-day query returns zero sources, the system must fall back to `_FALLBACK_TOPIC` (the existing hardcoded string) and log a warning.

### 4.6 Output validation and re-prompting

21. After selecting a topic (the deduplication-passing candidate), the system must validate it against these rules:
    - Word count is between 5 and 25 (inclusive).
    - The string does not match any of the known-bad patterns: `"football analysis"`, `"tactical trends"`, `"emerging football tactical trends"`, or any string ≤ 3 words.
22. If validation fails, the system must re-prompt the LLM once with the same prompt but with an explicit instruction to return a more specific phrase.
23. If the re-prompted response also fails validation, the system must attempt the next ranked candidate in order.
24. If no candidate passes validation (after re-prompting the last one), the system must raise a `TrendPassError` exception (a new exception class defined in `trend_pass.py`) so the caller can decide whether to abort or fall back.
25. `run_trend_pass` callers in `pipeline.py` must catch `TrendPassError`, log it, and fall back to `_FALLBACK_TOPIC` so the pipeline does not crash.

---

## 5. Non-Goals (Out of Scope)

- Manual or human-in-the-loop candidate selection (always auto-select #1).
- Changing the downstream retrieval, draft, critique, or revision passes.
- Storing candidate embeddings persistently in the `embeddings` table.
- Modifying the `reports` schema — topic deduplication queries existing `title` and `metadata` columns only.
- Adding a UI or API for reviewing trend candidates.
- Changing the frequency or scheduling of pipeline runs.

---

## 6. Design Considerations

### Prompt format for ranked candidates

The updated `TREND_USER_TEMPLATE` must request JSON output in the following shape so it can be parsed deterministically:

```json
[
  {
    "rank": 1,
    "topic": "<10–20 word phrase>",
    "justification": "<one sentence, ≤ 25 words>"
  },
  ...
]
```

The `max_tokens` budget for the trend LLM call must be raised to accommodate 3–5 candidates with justifications. A value of **600 tokens** is recommended.

### Source list format

Each line in the sources summary must follow this format:

```
[ARTICLE | 3 days ago] Title: <snippet up to 800 or 300 chars>
```

### Semantic similarity threshold

A cosine similarity threshold of **0.85** is used for deduplication (requirement 15). This value should be defined as a named constant `_DEDUP_SIMILARITY_THRESHOLD = 0.85` in `trend_pass.py`.

### Top-N snippet count

The number of sources that receive 800-char snippets should be a named constant `_LONG_SNIPPET_TOP_N = 10` in `trend_pass.py`.

---

## 7. Technical Considerations

- **Embedding calls** — deduplication requires N+1 embedding API calls at trend time (1 for the candidate + 1 per historical title). With 10 historical titles this is at most 11 calls per candidate. The existing `embed_chunks` infrastructure handles batching; a lightweight wrapper that accepts raw strings (not `Chunk` objects) may be needed.
- **JSON parsing** — the new LLM response is JSON. Use `json.loads()` with a `try/except` around parsing; if parsing fails, treat it as a validation failure and trigger the re-prompt flow.
- **`pipeline_runs.metadata`** — this column is already `JSONB DEFAULT '{}'`, so storing `trend_candidates` and `trend_lookback_days` requires no schema migration.
- **Cosine similarity** — compute as `1 - (euclidean distance of unit vectors)` or via dot product if embeddings are already unit-normalised (OpenAI `text-embedding-3-*` embeddings are unit vectors, so dot product == cosine similarity).
- **`TrendPassError`** — should subclass `Exception`; no additional fields required.

---

## 8. Success Metrics

| Metric | Target |
|---|---|
| Topic word count always in range 5–25 | 100 % of runs |
| Topic deduplication triggered at least once in first 20 runs | ≥ 1 occurrence |
| `trend_candidates` key present in `pipeline_runs.metadata` | 100 % of trend-pass runs |
| `trend_lookback_days` key present in `pipeline_runs.metadata` | 100 % of trend-pass runs |
| Pipeline never crashes due to a bad trend topic (TrendPassError caught) | 100 % of runs |
| Runs using 14-day window flagged in metadata | 100 % of such runs |

---

## 9. Open Questions

1. Should `_LONG_SNIPPET_TOP_N` (default 10) be configurable via `Settings` or left as a module-level constant?
2. Should all 3–5 candidates be subject to the deduplication check upfront (filtering the list before selecting #1), or should deduplication only be applied to the chosen candidate and fall through on failure? (This PRD assumes the latter for simplicity.)
3. Is 0.85 cosine similarity the right threshold, or should it be tuned empirically after the first few runs?
4. Should `TrendPassError` carry the list of failed candidates for downstream logging?
