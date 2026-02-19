# Tasks: Multi-Agent Research System

## Relevant Files

- `.github/workflows/report.yml` - Existing; no structural changes — schedule, secrets, and stage names are preserved.
- `src/generation/lead_agent.py` - New: LeadAgent; calls Opus to assess complexity, select subagent count, and produce task descriptions.
- `src/generation/sub_agent.py` - New: SubAgent with iterative search loop + parallel runner; returns SubAgentResult with search trajectory.
- `src/generation/synthesis_pass.py` - New: replaces draft_pass.py in the generation flow; merges all SubAgentResults into a single cited markdown.
- `src/generation/draft_pass.py` - Existing; **delete** after synthesis_pass.py is wired in (do not keep as dead code).
- `src/generation/critique_pass.py` - Existing; unchanged.
- `src/generation/revision_pass.py` - Existing; unchanged.
- `src/generation/prompts.py` - Existing; extended with lead agent, subagent, subagent evaluation, synthesis, and LLM judge prompt templates.
- `src/pipeline.py` - Existing; `run_generation()` updated to invoke LeadAgent + parallel SubAgents + SynthesisPass; `run_verification()` updated to call LLMJudge.
- `src/verification/llm_judge.py` - New: LLMJudge; single Claude Sonnet call; 5-criteria rubric; returns JudgeResult.
- `src/verification/scoring.py` - Existing; unchanged (LLMJudge result persisted directly in pipeline.py).
- `src/config.py` - Existing; add optional `anthropic_lead_model_id` field that defaults to `anthropic_model_id`.
- `tests/test_lead_agent.py` - Unit tests: complexity assessment, subagent count scaling, task description non-overlap.
- `tests/test_sub_agent.py` - Unit tests: iterative search loop, SubAgentResult format, error sentinel on failure.
- `tests/test_synthesis_pass.py` - Unit tests: synthesis merges chunks from multiple subagents with correct citations.
- `tests/test_llm_judge.py` - Unit tests: JudgeResult JSON parsing, score range validation, pass/fail threshold.
- `tests/test_parallel_generation.py` - Simulation test: fixed topic -> LeadAgent -> subagent task descriptions checked for overlap.

### Notes

- Unit tests should be placed alongside the code they test (e.g., `src/generation/lead_agent.py` and `tests/test_lead_agent.py`).
- Run tests with `pytest -q` from the project root.
- Each subagent thread must open and close its own `psycopg` connection — do not share connections across threads. The connection is reused across search rounds within the same subagent.
- `SubAgentResult` must be JSON-serialisable; write to `artifacts/reports/<pipeline_run_id>/subagents/<angle_slug>.json`.
- Lead agent planning response must be written to `artifacts/reports/<pipeline_run_id>/lead_agent_plan.json`.
- Citation format throughout is unchanged: `[S<source_id>:C<chunk_id>]`.
- The existing `research_pass.py` has 6 curated query areas (not 7). The 7th angle ("Background and prior work") is new.

## Instructions for Completing Tasks

IMPORTANT: As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` -> `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [x] 0.0 Create feature branch
  - [x] 0.1 Confirm branch `claude/multi-agent-research-system-pSQzg` is checked out (already exists)

- [x] 1.0 Extend config and prompts
  - [x] 1.1 Add `anthropic_lead_model_id: str` field to the `Settings` dataclass in `src/config.py`
  - [x] 1.2 Wire `anthropic_lead_model_id` in `load_settings()` via `_get_env("ANTHROPIC_LEAD_MODEL_ID", default=_md("ANTHROPIC_LEAD_MODEL_ID", "claude-opus-4-6"))` so it defaults to Opus but can be overridden
  - [x] 1.3 Add `LEAD_AGENT_SYSTEM` and `LEAD_AGENT_USER_TEMPLATE` string constants to `src/generation/prompts.py`; the user template must include: topic, complexity hint (from heuristic — the LLM may override), list of 7 canonical angles, scaling rules (1 subagent for simple, 2-4 moderate, 5-7 complex), instruction for the LLM to assess the actual complexity and explain its reasoning, and JSON output format for task descriptions. Each task description must include: `angle`, `angle_slug`, `objective`, `output_format`, `search_guidance` (initial broad query + suggested narrowing directions), and `task_boundaries` (what is explicitly out of scope for this subagent)
  - [x] 1.4 Add `SUBAGENT_SYSTEM` and `SUBAGENT_USER_TEMPLATE` constants to `prompts.py`; the user template receives: assigned angle, objective, search_guidance, task_boundaries, and the retrieved chunks JSON from the current search round; instructs the subagent to produce a summary with inline `[S<source_id>:C<chunk_id>]` citations from all chunks collected across rounds
  - [x] 1.5 Add `SUBAGENT_EVAL_SYSTEM` and `SUBAGENT_EVAL_USER_TEMPLATE` constants to `prompts.py`; this is the prompt used between search rounds within a subagent to evaluate retrieved chunks and decide whether to continue searching. The template receives: angle, objective, chunks retrieved so far, and current round number. It must return a JSON object with keys: `sufficient` (bool — whether enough evidence has been gathered), `gaps` (list[str] — what is still missing), `next_query` (str | null — the refined query for the next round, or null if sufficient)
  - [x] 1.6 Add `SYNTHESIS_SYSTEM` and `SYNTHESIS_USER_TEMPLATE` constants to `prompts.py`; the template receives: topic, per-subagent summaries (angle + summary text), deduplicated chunks JSON, and list of failed angles (if any). Instruct the model to follow the `research.md` output format: descriptive H1 title, numbered H2 sections with topic-specific angle headings, optional H3 subsections, bold key figures/statistics inline, tables for structured comparisons, `---` separators between major sections, and a standalone `## Conclusion` section. Failed angles must be called out explicitly in relevant sections or conclusion rather than silently omitted
  - [x] 1.7 Add `LLM_JUDGE_SYSTEM` and `LLM_JUDGE_USER_TEMPLATE` constants to `prompts.py`; the user template receives: final markdown report and source chunk texts; instructs the model to return a single JSON object with keys `factual_accuracy`, `citation_accuracy`, `completeness`, `source_quality`, `source_diversity` (each 0.0-1.0) and `overall_pass` (bool). Note: `source_diversity` replaces the original `tool_efficiency` — this system has a fixed search pattern, so tool selection is not evaluable; source diversity measures whether the report draws from varied sources
  - [x] 1.8 Add builder functions to `prompts.py`: `build_lead_agent_prompt(topic, complexity_hint) -> tuple[str, str]`, `build_subagent_prompt(angle, objective, search_guidance, task_boundaries, chunks_json) -> tuple[str, str]`, `build_subagent_eval_prompt(angle, objective, chunks_json, round_number) -> tuple[str, str]`, `build_synthesis_prompt(topic, subagent_summaries, chunks_json, failed_angles) -> tuple[str, str]`, `build_llm_judge_prompt(report_markdown, chunks_json) -> tuple[str, str]`

- [x] 2.0 Implement LeadAgent (orchestrator)
  - [x] 2.1 Create `src/generation/lead_agent.py`; define `TaskDescription` dataclass with fields: `angle` (str), `angle_slug` (str, kebab-case), `objective` (str), `output_format` (str), `search_guidance` (str — initial broad query + suggested narrowing directions), `task_boundaries` (str — what is out of scope for this subagent)
  - [x] 2.2 Define `LeadAgentResult` dataclass with fields: `topic` (str), `task_descriptions` (list[TaskDescription]), `subagent_count` (int), `complexity` (str — the LLM's assessed complexity, not just the heuristic), `complexity_hint` (str — the heuristic value passed as input), `planning_reasoning` (str — the LLM's explanation of its decomposition strategy)
  - [x] 2.3 Implement `_heuristic_complexity(topic: str) -> str` returning `"simple"`, `"moderate"`, or `"complex"` based on word count and presence of conjunctions/multi-part structure (<=4 words -> simple, 5-9 -> moderate, 10+ or contains "and"/"vs"/"across" -> complex). This is a **hint** passed to the lead agent prompt — the LLM determines the final complexity tier
  - [x] 2.4 Implement `run_lead_agent(topic: str, settings: Settings) -> LeadAgentResult`: call Claude (`settings.anthropic_lead_model_id`) with `build_lead_agent_prompt(topic, _heuristic_complexity(topic))`; parse the JSON response which must include `complexity` (str), `reasoning` (str), and `task_descriptions` (array); validate each task description has required keys including `task_boundaries`; raise `ValueError` if response is unparseable after one retry
  - [x] 2.5 Add duplicate-work guard in `run_lead_agent`: after parsing, check each pair of task descriptions for semantic overlap by comparing `objective` + `task_boundaries` text. Use embedding similarity via the existing OpenAI embeddings (same model as retrieval) with a threshold of 0.85 cosine similarity. Reject the later duplicate; log a warning for each rejection. Fall back to >60% token overlap if embedding call fails

- [x] 3.0 Implement SubAgent (worker) with iterative search loop
  - [x] 3.1 Create `src/generation/sub_agent.py`; define `SearchRound` dataclass with fields: `round_number` (int), `query` (str), `chunks_retrieved` (list[dict]), `chunk_count` (int), `evaluation` (dict — the LLM eval response: sufficient, gaps, next_query)
  - [x] 3.2 Define `SubAgentResult` dataclass with fields: `angle` (str), `angle_slug` (str), `chunks` (list[dict] — all unique chunks across all rounds), `summary` (str), `citations` (list[str]), `search_trajectory` (list[SearchRound]), `total_rounds` (int), `elapsed_s` (float), `input_tokens` (int), `output_tokens` (int), `error` (str | None); implement `to_dict() -> dict` for JSON serialisation
  - [x] 3.3 Implement `_run_search_round(query: str, connection, settings: Settings) -> list[RetrievedChunk]`: embed the query via OpenAI, call `hybrid_search()` with `top_k=8`, return results. This is a single retrieval call reusing the existing `research_pass.py` pattern
  - [x] 3.4 Implement `_evaluate_search_results(task: TaskDescription, all_chunks: list[dict], round_number: int, settings: Settings) -> dict`: call Claude Sonnet with `build_subagent_eval_prompt()` to evaluate whether enough evidence has been gathered. Return the parsed JSON with `sufficient`, `gaps`, `next_query` keys. If the LLM call fails, return `{"sufficient": True, "gaps": [], "next_query": null}` to gracefully stop the loop
  - [x] 3.5 Implement `run_subagent(task: TaskDescription, postgres_dsn: str, settings: Settings, max_search_rounds: int = 3) -> SubAgentResult`: open its own `psycopg` connection, then execute the iterative search loop:
    1. Round 1: use the task's `search_guidance` as the initial broad query -> `_run_search_round()`
    2. Call `_evaluate_search_results()` with all chunks collected so far
    3. If `sufficient` is True or `round_number >= max_search_rounds`, exit loop
    4. Otherwise, use `next_query` from evaluation as the query for the next round
    5. After loop: call Claude Sonnet with `build_subagent_prompt()` passing all collected chunks to produce the final summary with citations
    6. Record each round in `search_trajectory`
  - [x] 3.6 Wrap the entire body of `run_subagent` in a broad `except Exception` block; on failure set `error` field and return a sentinel `SubAgentResult` with empty `summary`, `chunks`, and `search_trajectory` so the pipeline continues
  - [x] 3.7 Implement `run_parallel_subagents(tasks: list[TaskDescription], postgres_dsn: str, settings: Settings, max_search_rounds: int = 3) -> list[SubAgentResult]` using `concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks))`; collect results preserving input order
  - [x] 3.8 After collecting results, raise `RuntimeError` if every `SubAgentResult` has a non-None `error` (no successful subagents); otherwise log a warning for each failed subagent and continue

- [x] 4.0 Implement SynthesisPass, wire generation in pipeline.py, and clean up dead code
  - [x] 4.1 Create `src/generation/synthesis_pass.py`; implement `run_synthesis_pass(topic: str, subagent_results: list[SubAgentResult], settings: Settings) -> str`: collect all chunks from successful subagents, deduplicate by chunk ID, build per-subagent summaries list (angle + summary text), identify failed angles, call Claude Sonnet with `build_synthesis_prompt()`, return cited markdown
  - [x] 4.2 Ensure `run_synthesis_pass` produces markdown using the `research.md` section structure (descriptive H1 title, numbered angle-based H2s, optional H3s, inline bold key figures, tables where useful, `---` separators, standalone `## Conclusion`) instead of the old hardcoded four-section draft format. Failed angles must be explicitly acknowledged in the output
  - [x] 4.3 Update `run_generation()` in `src/pipeline.py`: replace the `run_research_pass` + `run_draft_pass` calls with `run_lead_agent` -> `run_parallel_subagents` -> `run_synthesis_pass`; keep `run_critique_pass` and `run_revision_pass` calls unchanged. Remove the `run_research_pass` and `run_draft_pass` imports
  - [x] 4.4 In `run_generation()`, write each `SubAgentResult.to_dict()` to `artifacts/reports/<pipeline_run_id>/subagents/<angle_slug>.json` before calling synthesis. Also write the `LeadAgentResult` (including `planning_reasoning`) to `artifacts/reports/<pipeline_run_id>/lead_agent_plan.json`
  - [x] 4.5 Update the `stage_metrics` dict in `run_generation()` to include: `subagent_count`, `subagent_failures`, per-subagent `elapsed_s`, `total_rounds`, and token counts, replacing the old `query_count`/`context_chunks` keys
  - [x] 4.6 Delete `src/generation/draft_pass.py`. Update any imports in other files that reference it (check `pipeline.py` and test files). The synthesis pass fully replaces it

- [x] 5.0 Implement LLMJudge and wire into verification stage
  - [x] 5.1 Create `src/verification/llm_judge.py`; define `JudgeResult` dataclass with fields: `factual_accuracy` (float), `citation_accuracy` (float), `completeness` (float), `source_quality` (float), `source_diversity` (float), `overall_pass` (bool); add `average_score() -> float` method
  - [x] 5.2 Implement `run_llm_judge(report_markdown: str, chunk_texts: dict[int, str], settings: Settings) -> JudgeResult`: build chunks JSON from `chunk_texts`, call Claude using `build_llm_judge_prompt()` with `settings.anthropic_model_id` (Sonnet — judging factual accuracy requires strong reasoning; Haiku is too weak for reliable evaluation), parse JSON response, validate all five score keys are present and in [0.0, 1.0], return `JudgeResult`
  - [x] 5.3 Add a `try/except` around the entire `run_llm_judge` body; on failure log a warning and return a sentinel `JudgeResult` with all scores set to `0.0` and `overall_pass=False` so verification never hard-fails due to judge errors
  - [x] 5.4 Update `run_verification()` in `src/pipeline.py`: after the existing NLI scoring block, call `run_llm_judge(report_markdown, chunk_text_by_id, settings)` and persist result via `jsonb_set(metadata, '{llm_judge}', ...)` on the report row
  - [x] 5.5 Update `_persist_stage_cost_metrics` call in `run_verification()` to include `llm_judge_input_tokens`, `llm_judge_estimated_cost_usd` (using Sonnet pricing), and `llm_judge_pass` in the metrics dict

- [ ] 6.0 Write tests and run full suite
  - [ ] 6.1 Write `tests/test_lead_agent.py`: mock the Anthropic API; test that `_heuristic_complexity` returns the right tier for short/medium/long topics; test that `run_lead_agent` correctly parses a mocked LLM response with complexity assessment; test that the duplicate-work guard drops an overlapping task description (mock embedding similarity); test that `task_boundaries` is present in every TaskDescription
  - [ ] 6.2 Write `tests/test_sub_agent.py`: mock `psycopg.connect`, the Anthropic API, and OpenAI embeddings; test the iterative search loop: mock eval to return `sufficient=False` on round 1 with a `next_query`, then `sufficient=True` on round 2, and verify 2 search rounds occurred; test that a successful `run_subagent` returns a `SubAgentResult` with non-empty `summary` and `search_trajectory`; test that an API exception produces a sentinel result with `error` set; test that `run_parallel_subagents` raises `RuntimeError` when all subagents fail
  - [ ] 6.3 Write `tests/test_synthesis_pass.py`: mock the Anthropic API; test that `run_synthesis_pass` deduplicates chunks from two subagents that share a chunk ID; test that the returned markdown contains at least one citation in `[S\d+:C\d+]` format; test that failed angles are mentioned in the output
  - [ ] 6.4 Write `tests/test_llm_judge.py`: mock the Anthropic API to return valid JSON; test that `JudgeResult` fields are correctly parsed and `average_score()` is the mean of the 5 scores; test that `source_diversity` (not `tool_efficiency`) is used; test that a malformed JSON response returns the sentinel result without raising
  - [ ] 6.5 Write `tests/test_parallel_generation.py`: end-to-end simulation with mocked Anthropic API, mocked OpenAI embeddings, and mocked DB; run `run_lead_agent` for a fixed topic and assert no two `TaskDescription.objective` strings share >0.85 cosine similarity; verify `search_trajectory` is populated on each SubAgentResult
  - [ ] 6.6 Run `pytest -q` and fix any failures before marking this task complete
