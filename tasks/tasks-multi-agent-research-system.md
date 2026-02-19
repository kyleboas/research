# Tasks: Multi-Agent Research System

## Relevant Files

- `.github/workflows/report.yml` - Existing; no structural changes — schedule, secrets, and stage names are preserved.
- `src/generation/lead_agent.py` - New: LeadAgent class; selects subagent count based on complexity; produces task descriptions.
- `src/generation/sub_agent.py` - New: SubAgent execution function + parallel runner; returns SubAgentResult.
- `src/generation/synthesis_pass.py` - New: replaces draft_pass.py in the generation flow; merges all SubAgentResults into a single cited markdown.
- `src/generation/draft_pass.py` - Existing; superseded by synthesis_pass.py; kept but no longer called by pipeline.py.
- `src/generation/critique_pass.py` - Existing; unchanged.
- `src/generation/revision_pass.py` - Existing; unchanged.
- `src/generation/prompts.py` - Existing; extended with lead agent, subagent, and LLM judge prompt templates.
- `src/pipeline.py` - Existing; `run_generation()` updated to invoke LeadAgent + parallel SubAgents + SynthesisPass; `run_verification()` updated to call LLMJudge.
- `src/verification/llm_judge.py` - New: LLMJudge; single Claude call; 5-criteria rubric; returns JudgeResult.
- `src/verification/scoring.py` - Existing; unchanged (LLMJudge result persisted directly in pipeline.py).
- `src/config.py` - Existing; add optional `anthropic_lead_model_id` field that defaults to `anthropic_model_id`.
- `tests/test_lead_agent.py` - Unit tests: subagent count scaling rules, task description non-overlap.
- `tests/test_sub_agent.py` - Unit tests: SubAgentResult format, error sentinel on failure.
- `tests/test_synthesis_pass.py` - Unit tests: synthesis merges chunks from multiple subagents with correct citations.
- `tests/test_llm_judge.py` - Unit tests: JudgeResult JSON parsing, score range validation, pass/fail threshold.
- `tests/test_parallel_generation.py` - Simulation test: fixed topic → LeadAgent → subagent task descriptions checked for overlap.

### Notes

- Unit tests should be placed alongside the code they test (e.g., `src/generation/lead_agent.py` and `tests/test_lead_agent.py`).
- Run tests with `pytest -q` from the project root.
- Each subagent thread must open and close its own `psycopg` connection — do not share connections across threads.
- `SubAgentResult` must be JSON-serialisable; write to `artifacts/reports/<pipeline_run_id>/subagents/<angle_slug>.json`.
- Citation format throughout is unchanged: `[S<source_id>:C<chunk_id>]`.

## Instructions for Completing Tasks

IMPORTANT: As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [ ] 0.0 Create feature branch
  - [ ] 0.1 Confirm branch `claude/multi-agent-research-system-pSQzg` is checked out (already exists)

- [ ] 1.0 Extend config and prompts
  - [ ] 1.1 Add `anthropic_lead_model_id: str` field to the `Settings` dataclass in `src/config.py`
  - [ ] 1.2 Wire `anthropic_lead_model_id` in `load_settings()` via `_get_env("ANTHROPIC_LEAD_MODEL_ID", default=_md("ANTHROPIC_LEAD_MODEL_ID", "claude-opus-4-6"))` so it defaults to Opus but can be overridden
  - [ ] 1.3 Add `LEAD_AGENT_SYSTEM` and `LEAD_AGENT_USER_TEMPLATE` string constants to `src/generation/prompts.py`; the user template must include: topic, complexity hint, list of 7 canonical angles, scaling rules (1 subagent for simple, 2–4 moderate, 5–7 complex), and JSON output format for task descriptions
  - [ ] 1.4 Add `SUBAGENT_SYSTEM` and `SUBAGENT_USER_TEMPLATE` constants to `prompts.py`; the user template receives: assigned angle, objective, retrieved chunks JSON, and instruction to start with broad queries then narrow, producing a summary with inline `[S<source_id>:C<chunk_id>]` citations
  - [ ] 1.5 Add `LLM_JUDGE_SYSTEM` and `LLM_JUDGE_USER_TEMPLATE` constants to `prompts.py`; the user template receives: final markdown report and source chunk texts; instructs the model to return a single JSON object with keys `factual_accuracy`, `citation_accuracy`, `completeness`, `source_quality`, `tool_efficiency` (each 0.0–1.0) and `overall_pass` (bool)
  - [ ] 1.6 Add `build_lead_agent_prompt(topic, complexity_hint) -> tuple[str, str]`, `build_subagent_prompt(angle, objective, chunks_json) -> tuple[str, str]`, and `build_llm_judge_prompt(report_markdown, chunks_json) -> tuple[str, str]` builder functions to `prompts.py`

- [ ] 2.0 Implement LeadAgent (orchestrator)
  - [ ] 2.1 Create `src/generation/lead_agent.py`; define `TaskDescription` dataclass with fields: `angle` (str), `angle_slug` (str, kebab-case), `objective` (str), `output_format` (str), `search_guidance` (str)
  - [ ] 2.2 Define `LeadAgentResult` dataclass with fields: `topic` (str), `task_descriptions` (list[TaskDescription]), `subagent_count` (int), `complexity_hint` (str)
  - [ ] 2.3 Implement `_score_complexity(topic: str) -> str` returning `"simple"`, `"moderate"`, or `"complex"` based on word count and presence of conjunctions/multi-part structure (≤4 words → simple, 5–9 → moderate, 10+ or contains "and"/"vs"/"across" → complex)
  - [ ] 2.4 Implement `run_lead_agent(topic: str, settings: Settings) -> LeadAgentResult`: call Claude (`settings.anthropic_lead_model_id`) with `build_lead_agent_prompt()`; parse JSON array of task description objects; validate each has required keys; raise `ValueError` if response is unparseable after one retry
  - [ ] 2.5 Add duplicate-work guard in `run_lead_agent`: after parsing, reject any task description whose `objective` shares >60% token overlap with a previously accepted one; log a warning for each rejection

- [ ] 3.0 Implement SubAgent (worker) with parallel execution
  - [ ] 3.1 Create `src/generation/sub_agent.py`; define `SubAgentResult` dataclass with fields: `angle` (str), `angle_slug` (str), `chunks` (list[dict]), `summary` (str), `citations` (list[str]), `elapsed_s` (float), `input_tokens` (int), `output_tokens` (int), `error` (str | None); implement `to_dict() -> dict` for JSON serialisation
  - [ ] 3.2 Implement `run_subagent(task: TaskDescription, postgres_dsn: str, settings: Settings) -> SubAgentResult`: open its own `psycopg` connection, run `hybrid_rrf_search` for the task's `search_guidance` query (reuse `retrieval.py`), call Claude Sonnet (`settings.anthropic_model_id`) with `build_subagent_prompt()`, parse and return result
  - [ ] 3.3 Wrap the entire body of `run_subagent` in a broad `except Exception` block; on failure set `error` field and return a sentinel `SubAgentResult` with empty `summary` and `chunks` so the pipeline continues
  - [ ] 3.4 Implement `run_parallel_subagents(tasks: list[TaskDescription], postgres_dsn: str, settings: Settings) -> list[SubAgentResult]` using `concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks))`; collect results preserving input order
  - [ ] 3.5 After collecting results, raise `RuntimeError` if every `SubAgentResult` has a non-None `error` (no successful subagents); otherwise log a warning for each failed subagent and continue

- [ ] 4.0 Implement SynthesisPass and wire generation in pipeline.py
  - [ ] 4.1 Create `src/generation/synthesis_pass.py`; implement `run_synthesis_pass(topic: str, subagent_results: list[SubAgentResult], settings: Settings) -> str`: collect all chunks from successful subagents, deduplicate by chunk ID, assemble combined context JSON, call Claude Sonnet with `build_subagent_prompt()` adapted for synthesis, return cited markdown
  - [ ] 4.2 Ensure `run_synthesis_pass` produces a markdown report with the same section structure as `draft_pass.py` (Executive Summary, Key Findings, Evidence Notes, Open Questions) so downstream critique and revision passes receive a compatible input
  - [ ] 4.3 Update `run_generation()` in `src/pipeline.py`: replace the `run_research_pass` + `run_draft_pass` calls with `run_lead_agent` → `run_parallel_subagents` → `run_synthesis_pass`; keep `run_critique_pass` and `run_revision_pass` calls unchanged
  - [ ] 4.4 In `run_generation()`, write each `SubAgentResult.to_dict()` to `artifacts/reports/<pipeline_run_id>/subagents/<angle_slug>.json` before calling synthesis
  - [ ] 4.5 Update the `stage_metrics` dict in `run_generation()` to include: `subagent_count`, `subagent_failures`, per-subagent `elapsed_s` and token counts, replacing the old `query_count`/`context_chunks` keys

- [ ] 5.0 Implement LLMJudge and wire into verification stage
  - [ ] 5.1 Create `src/verification/llm_judge.py`; define `JudgeResult` dataclass with fields: `factual_accuracy` (float), `citation_accuracy` (float), `completeness` (float), `source_quality` (float), `tool_efficiency` (float), `overall_pass` (bool); add `average_score() -> float` method
  - [ ] 5.2 Implement `run_llm_judge(report_markdown: str, chunk_texts: dict[int, str], settings: Settings) -> JudgeResult`: build chunks JSON from `chunk_texts`, call Claude using `build_llm_judge_prompt()` with `settings.anthropic_small_model_id` (Haiku, to keep cost low), parse JSON response, validate all five score keys are present and in [0.0, 1.0], return `JudgeResult`
  - [ ] 5.3 Add a `try/except` around the entire `run_llm_judge` body; on failure log a warning and return a sentinel `JudgeResult` with all scores set to `0.0` and `overall_pass=False` so verification never hard-fails due to judge errors
  - [ ] 5.4 Update `run_verification()` in `src/pipeline.py`: after the existing NLI scoring block, call `run_llm_judge(report_markdown, chunk_text_by_id, settings)` and persist result via `jsonb_set(metadata, '{llm_judge}', ...)` on the report row
  - [ ] 5.5 Update `_persist_stage_cost_metrics` call in `run_verification()` to include `llm_judge_input_tokens`, `llm_judge_estimated_cost_usd` (using Haiku pricing), and `llm_judge_pass` in the metrics dict

- [ ] 6.0 Write tests and run full suite
  - [ ] 6.1 Write `tests/test_lead_agent.py`: mock the Anthropic API; test that `_score_complexity` returns the right tier for short/medium/long topics; test that `run_lead_agent` returns 1/3/6 task descriptions for simple/moderate/complex; test that the duplicate-work guard drops an overlapping task description
  - [ ] 6.2 Write `tests/test_sub_agent.py`: mock `psycopg.connect` and the Anthropic API; test that a successful `run_subagent` returns a `SubAgentResult` with non-empty `summary`; test that an API exception produces a sentinel result with `error` set; test that `run_parallel_subagents` raises `RuntimeError` when all subagents fail
  - [ ] 6.3 Write `tests/test_synthesis_pass.py`: mock the Anthropic API; test that `run_synthesis_pass` deduplicates chunks from two subagents that share a chunk ID; test that the returned markdown contains at least one citation in `[S\d+:C\d+]` format
  - [ ] 6.4 Write `tests/test_llm_judge.py`: mock the Anthropic API to return valid JSON; test that `JudgeResult` fields are correctly parsed and `average_score()` is the mean; test that a malformed JSON response returns the sentinel result without raising
  - [ ] 6.5 Write `tests/test_parallel_generation.py`: end-to-end simulation with mocked Anthropic API and mocked DB; run `run_lead_agent` for a fixed topic and assert no two `TaskDescription.objective` strings share >60% token overlap
  - [ ] 6.6 Run `pytest -q` and fix any failures before marking this task complete
