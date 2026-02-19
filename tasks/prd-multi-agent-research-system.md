# PRD: Multi-Agent Research System (report.yml)

## 1. Introduction / Overview

The current pipeline (`report.yml`) generates weekly research reports using a **sequential** 5-pass LLM approach: trend → research → draft → critique → revision. Each pass runs one at a time, and the "research" pass fires 7 curated queries one after another.

This feature updates the existing `report.yml` workflow and its underlying Python generation modules to implement a **multi-agent orchestrator-worker architecture** inspired by Anthropic's internal Research system (documented in `docs/how.md`). A lead agent (Claude Opus) coordinates parallel subagents (Claude Sonnet), each independently searching a distinct research angle, then hands results to a synthesis pass that produces the final report.

No new workflow file is created — all changes land in `.github/workflows/report.yml` and the corresponding Python modules under `src/`.

---

## 2. Goals

1. **Parallelise research**: The 7 curated research queries must run as parallel subagents (via Python `concurrent.futures`) rather than sequentially, reducing generation wall-clock time.
2. **Teach the orchestrator to delegate**: The lead agent prompt must produce explicit, unambiguous task descriptions for each subagent (objective, output format, tool guidance, task boundaries).
3. **Scale effort to query complexity**: The lead agent must select 1–10 subagents based on topic complexity, guided by explicit scaling rules embedded in the prompt.
4. **LLM-as-judge evaluation**: A new verification pass must score each report against a 5-criteria rubric (factual accuracy, citation accuracy, completeness, source quality, tool efficiency) using a single LLM call returning scores 0.0–1.0.
5. **Acceptance test**: The pipeline must run end-to-end in CI with measurable quality output (verification score ≥ 70, wall-clock generation time ≤ current sequential baseline × 0.5).

---

## 3. User Stories

- **As a pipeline operator**, I want parallel subagents to explore different research angles simultaneously, so that a complex topic is covered more thoroughly in less time.
- **As a pipeline operator**, I want the orchestrator to allocate fewer subagents to simple queries (single fact lookups) and more to complex queries (multi-dimensional topics), so tokens are spent efficiently.
- **As a developer**, I want each subagent to receive an explicit, non-overlapping task description so that subagents don't duplicate searches.
- **As a developer**, I want an LLM-as-judge evaluation score attached to every report so I can track quality trends over time.
- **As a pipeline operator**, I want `report.yml` to remain resumable from any stage (generation, verification, delivery) so that partial failures don't require full reruns.

---

## 4. Functional Requirements

1. The system must update `.github/workflows/report.yml` to retain its existing stage structure (init → generation → verification → delivery) and `from_stage` resume capability unchanged.
2. The system must implement a `LeadAgent` class in `src/generation/lead_agent.py` that:
   a. Accepts a topic string and a complexity score.
   b. Selects N subagents (1 for simple, 2–4 for moderate, 5–7 for complex) based on explicit scaling rules.
   c. Produces a structured task description for each subagent including: objective, output format, assigned query angle, and search guidance.
3. The system must implement a `SubAgent` class (or function) in `src/generation/sub_agent.py` that:
   a. Receives a task description from the lead agent.
   b. Executes its assigned research query against the hybrid retrieval system.
   c. Returns a `SubAgentResult` (assigned angle, retrieved chunks, summary, citations).
4. The system must run all subagents in parallel using `concurrent.futures.ThreadPoolExecutor`.
5. The system must preserve the existing 7 research angles as the canonical subagent specialisations:
   - Latest developments and announcements
   - Technical methods and implementation details
   - Limitations, risks, and failure modes
   - Business, product, and ecosystem implications
   - Notable quantitative claims and benchmarks
   - Open questions and unresolved debates
   - Background and prior work (new, replaces current catch-all)
6. The system must implement a `SynthesisPass` in `src/generation/synthesis_pass.py` that replaces `draft_pass.py`; it receives all `SubAgentResult` objects and produces a single cited markdown report.
7. The system must retain the existing `CritiquePass` and `RevisionPass` unchanged.
8. The system must implement an `LLMJudge` class in `src/verification/llm_judge.py` that:
   a. Accepts the final revised markdown and the source chunks used.
   b. Issues a single Claude API call with a rubric covering: factual accuracy, citation accuracy, completeness, source quality, tool efficiency.
   c. Returns a `JudgeResult` with per-criterion scores (0.0–1.0) and an overall pass/fail.
9. The system must persist the `JudgeResult` to the `reports` table alongside the existing token-overlap verification score.
10. The system must log each subagent's wall-clock time, token count, and chunk count to the `pipeline_runs.cost_estimate_json` field.
11. The lead agent prompt must include explicit scaling rules: simple (1 subagent, 3–10 tool calls), moderate (2–4 subagents, 10–15 calls each), complex (5+ subagents, clearly divided).
12. Each subagent prompt must instruct the agent to start with broad queries and progressively narrow, mirroring the "start wide, then narrow" principle from `how.md`.

---

## 5. Non-Goals (Out of Scope)

- **True async execution** (Python `asyncio` with streaming): `ThreadPoolExecutor` is sufficient; `asyncio` is not required.
- **Subagent-to-subagent communication**: Subagents do not coordinate with each other directly; only the lead agent aggregates results.
- **Rainbow deployments**: Deployment strategy changes are out of scope.
- **Extended thinking mode**: Claude's extended thinking feature is not used; standard prompting is sufficient.
- **MCP tool integration**: No new tool integrations (Slack search, Google Drive, etc.); only the existing hybrid retrieval is used.
- **Replacing `ingestion.yml` or `embedding` stages**: Only the generation stage and onwards changes.
- **Dynamic topic decomposition**: The lead agent selects which of the 7 fixed angles to activate; it does not invent new angles.

---

## 6. Design Considerations

- **Backwards compatibility**: `report.yml`'s trigger schedule, secrets, and stage names must remain unchanged; only the Python modules it calls are updated.
- **Subagent result format**: `SubAgentResult` must be JSON-serialisable so it can be stored as an artifact for debugging.
- **Context isolation**: Each subagent gets its own context window with only its assigned chunks — no shared state between subagents during retrieval.
- **Lead agent model**: Claude Opus (`ANTHROPIC_MODEL_ID` or a new `ANTHROPIC_LEAD_MODEL_ID` env var). Subagents use Sonnet (`anthropic_model_id`). Critique uses Haiku (`anthropic_small_model_id`) — unchanged.
- **Prompt iteration**: Follow the "think like your agents" principle — include a simulation test in `tests/` that replays a fixed topic through the lead agent and checks that subagent task descriptions are non-overlapping and non-vague.

---

## 7. Technical Considerations

- **Thread safety**: The Anthropic SDK client is thread-safe; no locking is needed for parallel API calls.
- **Database connections**: Use a connection-per-thread pattern (each subagent opens and closes its own `psycopg` connection) to avoid pool contention.
- **Error isolation**: A subagent failure must not abort the pipeline; the lead agent must receive an error sentinel and proceed with remaining subagents (minimum 1 successful subagent required).
- **Artifact storage**: Each subagent's `SubAgentResult` JSON is written to `artifacts/reports/<pipeline_run_id>/subagents/<angle_slug>.json` and uploaded as a GitHub Actions artifact.
- **LLM judge prompt**: Single call, structured JSON output (`{"factual_accuracy": 0.9, "citation_accuracy": 0.8, ...}`). Use `response_format` / JSON mode where available.

---

## 8. Success Metrics

| Metric | Target |
|---|---|
| End-to-end pipeline run completes in CI | ✅ Pass |
| Generation wall-clock time | ≤ 50% of current sequential baseline |
| LLM judge overall score | ≥ 0.70 on the first acceptance test run |
| Token-overlap verification score | ≥ current baseline (no regression) |
| Subagent task description overlap | 0 duplicate queries detected in simulation test |
| CI: all existing tests pass | ✅ No regressions |

---

## 9. Open Questions

1. Should `ANTHROPIC_LEAD_MODEL_ID` be a new required secret, or should it default to the existing `ANTHROPIC_MODEL_ID`? (Recommended: default to existing, add as optional override.)
2. What is the current sequential generation wall-clock baseline (in minutes) to measure the 50% improvement against? This needs to be measured before implementation.
3. Should the LLM judge run as a separate GitHub Actions job (like the existing `verification` job) or inline within the `verification` Python stage?
4. If all subagents fail for a given angle, should the synthesis pass attempt to fill the gap with a direct retrieval query, or skip that section?
