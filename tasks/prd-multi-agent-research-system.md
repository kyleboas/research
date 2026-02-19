# PRD: Multi-Agent Research System (report.yml)

## 1. Introduction / Overview

The current pipeline (`report.yml`) generates weekly research reports using a **sequential** 5-pass LLM approach: trend → research → draft → critique → revision. Each pass runs one at a time, and the "research" pass fires 6 curated queries one after another with a single retrieval call per query.

This feature updates the existing `report.yml` workflow and its underlying Python generation modules to implement a **multi-agent orchestrator-worker architecture** inspired by Anthropic's internal Research system (documented in `docs/how.md`). A lead agent (Claude Opus) coordinates parallel subagents (Claude Sonnet), each independently **searching iteratively** across a distinct research angle — starting with broad queries, evaluating results, then narrowing — before handing results to a synthesis pass that produces the final report.

No new workflow file is created — all changes land in `.github/workflows/report.yml` and the corresponding Python modules under `src/`.

### Known simplifications vs. `how.md`

This PRD deliberately scopes down several aspects of Anthropic's production system for a first iteration. These are documented here so future work can close the gaps:

- **Fixed angles instead of dynamic decomposition**: The lead agent selects from 7 canonical angles rather than inventing new ones per query. This limits adaptability for novel queries but simplifies implementation and evaluation.
- **No extended thinking**: `how.md` credits extended thinking with "improved instruction-following, reasoning, and efficiency." This can be enabled for the lead agent planning step in a follow-up.
- **No agent self-improvement loop**: `how.md` describes a meta-learning cycle where agents diagnose prompt failures and suggest improvements. No equivalent feedback mechanism is included here.
- **No human evaluation workflow**: `how.md` emphasises that "human evaluation catches what automation misses." A human review checkpoint should be added once the automated pipeline is stable.
- **Subagent count capped at 7**: `how.md` scales to 10+ subagents for complex queries. With 7 fixed angles, this system cannot exceed 7. Dynamic decomposition would remove the cap.

---

## 2. Goals

1. **Parallelise research with iterative search**: Each subagent must independently search its angle via a multi-turn loop (query → retrieve → evaluate → refine → retrieve again), not a single retrieval call. Subagents run in parallel via `concurrent.futures`.
2. **Teach the orchestrator to delegate**: The lead agent prompt must produce explicit, unambiguous task descriptions for each subagent (objective, output format, tool guidance, task boundaries). Descriptions must be detailed enough that subagents never duplicate each other's work.
3. **Scale effort to query complexity**: The lead agent LLM call must assess topic complexity (using a word-count heuristic as a hint, not the final answer) and select 1–7 subagents based on explicit scaling rules embedded in the prompt.
4. **LLM-as-judge evaluation**: A new verification pass must score each report against a 5-criteria rubric (factual accuracy, citation accuracy, completeness, source quality, source diversity) using a single LLM call returning scores 0.0–1.0.
5. **Structured tracing**: Each subagent's full search trajectory (queries issued, chunks retrieved per round, LLM evaluation reasoning) must be persisted as JSON artifacts for debugging agent behaviour.
6. **Acceptance test**: The pipeline must run end-to-end in CI with measurable quality output (verification score ≥ 70, wall-clock generation time ≤ current sequential baseline × 0.5).

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
2. The system must implement a `LeadAgent` in `src/generation/lead_agent.py` that:
   a. Accepts a topic string.
   b. Calls the lead agent LLM (Claude Opus) to assess complexity and select N subagents (1 for simple, 2–4 for moderate, 5–7 for complex). A word-count/conjunction heuristic is passed as a `complexity_hint` in the prompt, but the LLM determines the final complexity tier and subagent count.
   c. Produces a structured task description for each subagent including: objective, output format, assigned query angle, search guidance (initial broad query + suggested narrowing directions), and task boundaries (what is explicitly out of scope for this subagent).
3. The system must implement a `SubAgent` in `src/generation/sub_agent.py` that:
   a. Receives a task description from the lead agent.
   b. Executes an iterative search loop: (1) issue an initial broad retrieval query from the task's `search_guidance`, (2) call the LLM to evaluate retrieved chunks and decide whether to refine/continue, (3) issue narrower follow-up queries based on gaps identified, (4) repeat for up to `max_search_rounds` (default 3). Each round uses `hybrid_search` via its own `psycopg` connection.
   c. After the search loop completes, calls the LLM once to produce a summary with inline `[S<id>:C<id>]` citations from all collected chunks.
   d. Returns a `SubAgentResult` (assigned angle, all retrieved chunks across rounds, summary, citations, search trajectory log).
4. The system must run all subagents in parallel using `concurrent.futures.ThreadPoolExecutor`.
5. The system must preserve the existing research angles as the canonical subagent specialisations, expanded from 6 to 7:
   - Latest developments and announcements (existing)
   - Technical methods and implementation details (existing)
   - Limitations, risks, and failure modes (existing)
   - Business, product, and ecosystem implications (existing)
   - Notable quantitative claims and benchmarks (existing)
   - Open questions and unresolved debates (existing)
   - Background and prior work (new)
6. The system must implement a `SynthesisPass` in `src/generation/synthesis_pass.py` that replaces `draft_pass.py` in the pipeline; it receives all `SubAgentResult` objects and produces a single cited markdown report. `draft_pass.py` must be deleted (not kept as dead code).
7. The system must retain the existing `CritiquePass` and `RevisionPass` unchanged in implementation and interface; they must accept and revise the synthesis markdown regardless of section names, so no fixed four-section compatibility requirement is imposed.
8. The system must implement an `LLMJudge` in `src/verification/llm_judge.py` that:
   a. Accepts the final revised markdown and the source chunks used.
   b. Issues a single Claude API call (using `anthropic_model_id` / Sonnet) with a rubric covering: factual accuracy, citation accuracy, completeness, source quality, source diversity.
   c. Returns a `JudgeResult` with per-criterion scores (0.0–1.0) and an overall pass/fail.
   d. Note: `tool_efficiency` is replaced by `source_diversity` because subagents in this system have a fixed search pattern (iterative retrieval) — there is no tool selection to evaluate. `source_diversity` measures whether the report draws from a variety of distinct sources rather than over-relying on one or two.
9. The system must persist the `JudgeResult` to the `reports` table alongside the existing token-overlap verification score.
10. The system must log each subagent's wall-clock time, token count, chunk count, and search trajectory to `pipeline_runs.cost_estimate_json`. Each `SubAgentResult` artifact must include the full search trajectory (queries issued per round, chunks retrieved per round, LLM evaluation reasoning per round).
11. The lead agent prompt must include explicit scaling rules: simple (1 subagent), moderate (2–4 subagents), complex (5–7 subagents, clearly divided responsibilities).
12. Each subagent must implement the "start wide, then narrow" principle architecturally: the first search round uses a broad query; subsequent rounds use queries refined by the LLM based on gaps in the initial results. This is enforced by the search loop (requirement 3b), not just by prompt instructions.
13. The synthesis pass must have its own dedicated prompt template (`SYNTHESIS_SYSTEM` / `SYNTHESIS_USER_TEMPLATE` / `build_synthesis_prompt()`) — it must not reuse the subagent prompt. The synthesis prompt receives: topic, per-subagent summaries, deduplicated chunks JSON, and instructions to merge angles into a coherent report using the `research.md` format: a descriptive H1 title, numbered H2 sections named after the chosen research angles (topic-specific headings), optional H3 subsections where useful, bold key figures/statistics inline, tables for structured comparisons, `---` separators between major sections, and a standalone `## Conclusion` section.

---

## 5. Non-Goals (Out of Scope)

- **True async execution** (Python `asyncio` with streaming): `ThreadPoolExecutor` is sufficient; `asyncio` is not required.
- **Subagent-to-subagent communication**: Subagents do not coordinate with each other directly; only the lead agent aggregates results.
- **Rainbow deployments**: Deployment strategy changes are out of scope.
- **Extended thinking mode**: Claude's extended thinking feature is not used in this iteration. `how.md` credits it with improved reasoning and efficiency — consider enabling for the lead agent planning step in a follow-up.
- **MCP tool integration**: No new tool integrations (Slack search, Google Drive, etc.); only the existing hybrid retrieval is used.
- **Replacing `ingestion.yml` or `embedding` stages**: Only the generation stage and onwards changes.
- **Dynamic topic decomposition**: The lead agent selects which of the 7 fixed angles to activate; it does not invent new angles. This limits the system to 7 subagents maximum and reduces adaptability for novel queries.
- **Agent self-improvement**: No meta-learning loop where agents diagnose and fix prompt failures. This is a `how.md` principle deferred to a future iteration.
- **Human evaluation workflow**: No structured human review process. The first automated reports should be manually reviewed before trusting the LLM judge scores.

---

## 6. Design Considerations

- **Backwards compatibility**: `report.yml`'s trigger schedule, secrets, and stage names must remain unchanged; only the Python modules it calls are updated.
- **Subagent result format**: `SubAgentResult` must be JSON-serialisable and include the full search trajectory (queries, chunks per round, evaluation reasoning) so it can be stored as an artifact for debugging. This aligns with `how.md`'s emphasis on "full production tracing."
- **Context isolation**: Each subagent gets its own context window — no shared state between subagents during retrieval. Each subagent builds its own context through iterative search.
- **Subagent output to filesystem**: Following `how.md`'s principle of "subagent output to a filesystem to minimize the game of telephone," each subagent writes its `SubAgentResult` to disk. The synthesis pass reads from these artifacts rather than receiving results through in-memory coordinator passing. This prevents information loss and reduces token overhead.
- **Lead agent model**: Claude Opus (`ANTHROPIC_LEAD_MODEL_ID` env var, defaulting to `anthropic_model_id`). Subagents use Sonnet (`anthropic_model_id`). Critique uses Haiku (`anthropic_small_model_id`) — unchanged.
- **LLM judge model**: Sonnet (`anthropic_model_id`), not Haiku. Judging factual accuracy and citation correctness requires strong reasoning. Using Haiku risks producing unreliable evaluation scores.
- **Prompt iteration**: Follow the "think like your agents" principle — include a simulation test in `tests/` that replays a fixed topic through the lead agent and checks that subagent task descriptions are non-overlapping and non-vague.
- **Error recovery**: A failed subagent returns a sentinel result, but the lead agent's task description for that angle is logged in the search trajectory. If a majority of subagents fail, the synthesis pass should note which angles have no coverage rather than silently omitting them.

---

## 7. Technical Considerations

- **Thread safety**: The Anthropic SDK client is thread-safe; no locking is needed for parallel API calls.
- **Database connections**: Use a connection-per-thread pattern (each subagent opens and closes its own `psycopg` connection) to avoid pool contention. Each search round within a subagent reuses the same connection.
- **Error isolation**: A subagent failure must not abort the pipeline; the subagent must return an error sentinel and the pipeline proceeds with remaining subagents (minimum 1 successful subagent required).
- **Artifact storage**: Each subagent's `SubAgentResult` JSON (including search trajectory) is written to `artifacts/reports/<pipeline_run_id>/subagents/<angle_slug>.json` and uploaded as a GitHub Actions artifact. The lead agent's full planning response is written to `artifacts/reports/<pipeline_run_id>/lead_agent_plan.json`.
- **LLM judge prompt**: Single call, structured JSON output (`{"factual_accuracy": 0.9, "citation_accuracy": 0.8, ...}`). Use `response_format` / JSON mode where available.
- **Token budget awareness**: Each subagent's search loop is bounded by `max_search_rounds` (default 3) to prevent runaway token usage. The total token cost per subagent is tracked and included in `cost_estimate_json`.

---

## 8. Success Metrics

| Metric | Target |
|---|---|
| End-to-end pipeline run completes in CI | ✅ Pass |
| Generation wall-clock time | ≤ 50% of current sequential baseline |
| LLM judge overall score | ≥ 0.70 on the first acceptance test run |
| Token-overlap verification score | ≥ current baseline (no regression) |
| Subagent task description overlap | 0 duplicate queries detected in simulation test |
| Subagent search depth | Average ≥ 2 search rounds per subagent (iterative search is being used) |
| Search trajectory artifacts | Every successful subagent produces a complete trajectory JSON |
| CI: all existing tests pass | ✅ No regressions |

---

## 9. Open Questions

1. Should `ANTHROPIC_LEAD_MODEL_ID` be a new required secret, or should it default to the existing `ANTHROPIC_MODEL_ID`? (Recommended: default to existing, add as optional override.)
2. What is the current sequential generation wall-clock baseline (in minutes) to measure the 50% improvement against? This needs to be measured before implementation.
3. Should the LLM judge run as a separate GitHub Actions job (like the existing `verification` job) or inline within the `verification` Python stage?
4. If all subagents fail for a given angle, should the synthesis pass attempt to fill the gap with a direct retrieval query, or skip that section?
5. Should `max_search_rounds` be configurable per-subagent (e.g., the lead agent could assign more rounds to complex angles), or should it be a global default?
6. Should the subagent's LLM evaluation step between search rounds use Haiku (cheaper, faster) or Sonnet (better reasoning about what's missing)?
