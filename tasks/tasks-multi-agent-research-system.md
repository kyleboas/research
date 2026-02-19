# Tasks: Multi-Agent Research System

## Relevant Files

- `.github/workflows/report.yml` - Existing; no structural changes — schedule, secrets, and stage names are preserved.
- `src/generation/lead_agent.py` - New: LeadAgent class; selects subagent count based on complexity; produces task descriptions.
- `src/generation/sub_agent.py` - New: SubAgent class; executes one research angle; returns SubAgentResult.
- `src/generation/synthesis_pass.py` - New: replaces draft_pass.py in the generation flow; merges all SubAgentResults into a single cited markdown.
- `src/generation/draft_pass.py` - Existing; superseded by synthesis_pass.py; kept but no longer called by pipeline.py.
- `src/generation/critique_pass.py` - Existing; unchanged.
- `src/generation/revision_pass.py` - Existing; unchanged.
- `src/generation/prompts.py` - Existing; extended with lead agent and subagent prompt templates.
- `src/pipeline.py` - Existing; `run_generation()` updated to invoke LeadAgent + parallel SubAgents + SynthesisPass.
- `src/verification/llm_judge.py` - New: LLMJudge class; single Claude call; 5-criteria rubric; returns JudgeResult.
- `src/verification/scoring.py` - Existing; updated to persist JudgeResult alongside token-overlap score.
- `src/config.py` - Existing; add optional `ANTHROPIC_LEAD_MODEL_ID` env var.
- `tests/test_lead_agent.py` - Unit tests: subagent count scaling rules, task description non-overlap.
- `tests/test_sub_agent.py` - Unit tests: SubAgent result format, error sentinel on failure.
- `tests/test_synthesis_pass.py` - Unit tests: synthesis merges chunks from multiple subagents with correct citations.
- `tests/test_llm_judge.py` - Unit tests: JudgeResult parsing, score range validation.
- `tests/test_parallel_generation.py` - Integration/simulation test: fixed topic → lead agent → subagent task descriptions checked for overlap and vagueness.

### Notes

- Unit tests should be placed alongside the code they test (e.g., `src/generation/lead_agent.py` and `tests/test_lead_agent.py`).
- Run tests with `pytest -q` from the project root.
- Each subagent uses its own `psycopg` connection; do not share connections across threads.
- `SubAgentResult` must be JSON-serialisable; write to `artifacts/reports/<pipeline_run_id>/subagents/<angle_slug>.json`.

## Instructions for Completing Tasks

IMPORTANT: As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [ ] 0.0 Create feature branch
  - [ ] 0.1 Create and checkout branch `claude/multi-agent-research-system-pSQzg`

- [ ] 1.0 Extend config and prompts
- [ ] 2.0 Implement LeadAgent (orchestrator)
- [ ] 3.0 Implement SubAgent (worker) with parallel execution
- [ ] 4.0 Implement SynthesisPass (replaces DraftPass in multi-agent flow)
- [ ] 5.0 Implement LLMJudge and wire into verification stage
- [ ] 6.0 Write tests and run full suite
