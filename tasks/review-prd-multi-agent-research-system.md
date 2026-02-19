# Review: PRD & Tasks for Multi-Agent Research System

**PR:** #51 (`claude/multi-agent-research-system-pSQzg`)
**Reference:** `docs/how.md` (Anthropic's multi-agent Research system writeup)

---

## Summary

The PRD correctly identifies the core architectural pattern (orchestrator-worker with parallel subagents) and several key principles from Anthropic's approach. However, there are significant gaps between what the PRD proposes and what `how.md` actually describes. The most critical gaps relate to how subagents work, how complexity is assessed, and several principles that are either explicitly excluded or silently omitted.

The gaps fall into three categories:
1. **Architectural** -- the proposed system doesn't replicate how Anthropic's subagents actually operate
2. **Missing principles** -- several key practices from `how.md` are absent from the PRD
3. **Task-level errors** -- specific implementation tasks contain mistakes or mismatches

---

## Critical Architectural Gaps

### 1. Subagents don't actually search -- they receive pre-retrieved chunks

This is the single largest divergence from Anthropic's approach.

**What `how.md` says:** Subagents "iteratively use search tools to gather information." They are "intelligent filters" that autonomously search, evaluate results, and refine queries. The essence of their value is that each subagent has "its own context window" and performs "independent investigations."

**What the PRD proposes:** Task 3.2 has `run_subagent()` call `hybrid_rrf_search` once, then call Claude once to summarize. The subagent receives a single batch of chunks and produces a summary. There is no iteration, no query refinement, no multi-step search.

**Why this matters:** The core value proposition of multi-agent search is that subagents compress a large information space through iterative exploration. A subagent that makes one retrieval call and one LLM call is functionally equivalent to the current sequential pass with extra overhead. The parallelism saves wall-clock time, but the quality improvement from multi-agent search -- finding information that a single linear pass would miss -- is lost.

**To close the gap:** Each subagent needs a search loop: query -> retrieve -> evaluate (via LLM) -> decide whether to refine/continue -> query again. This requires the subagent to make multiple retrieval calls per invocation, guided by its LLM reasoning about what's missing.

### 2. No iterative "start wide, then narrow" in the architecture

**What `how.md` says:** "Search strategy should mirror expert human research: explore the landscape before drilling into specifics." Subagents "start with short, broad queries, evaluate what's available, then progressively narrow focus." Subagents also "use interleaved thinking after tool results to evaluate quality, identify gaps, and refine their next query."

**What the PRD proposes:** Task 1.4 instructs the subagent prompt to say "start with broad queries then narrow," but the architecture only supports one retrieval call per subagent (task 3.2). You cannot start wide and then narrow if you only make one query. The instruction is cosmetic without architectural support.

**To close the gap:** This is the same fix as gap #1. Subagents need a multi-turn search loop, not a single-shot retrieve-and-summarize pattern.

### 3. Fixed angles prevent the lead agent from actually "developing a strategy"

**What `how.md` says:** The lead agent "analyzes the query, develops a strategy, and spawns subagents to explore different aspects simultaneously." It "decomposes queries into subtasks."

**What the PRD proposes:** The 7 research angles are hardcoded (requirement 5), and "dynamic topic decomposition" is explicitly a Non-Goal. The lead agent only selects which of the 7 fixed angles to activate and how many subagents to spawn.

**Why this matters:** For a football/sports research system with a narrow domain, fixed angles may be reasonable. But this significantly limits the system's ability to handle queries that don't decompose neatly into those 7 categories. The lead agent isn't really "developing a strategy" -- it's selecting from a menu. This is a deliberate tradeoff the PRD makes, but it should be acknowledged as a significant simplification of Anthropic's approach, not presented as equivalent.

### 4. Complexity scoring uses string heuristics instead of semantic analysis

**What `how.md` says:** Scaling rules map to query complexity -- "simple fact-finding requires just 1 agent," "direct comparisons might need 2-4 subagents," "complex research might use more than 10 subagents."

**What task 2.3 proposes:** `_score_complexity()` uses word count and presence of conjunctions: <=4 words is simple, 5-9 is moderate, 10+ or contains "and"/"vs"/"across" is complex.

**Why this matters:** "List all S&P 500 IT company board members" is 8 words but extremely complex. "The socioeconomic implications of emerging artificial intelligence across domains" is wordy but might be moderate. Word count is a poor proxy for query complexity. Anthropic's system presumably uses the LLM itself to assess complexity, since the lead agent is already analyzing the query.

**To close the gap:** Have the lead agent (Claude Opus) determine complexity as part of its planning step, rather than using a regex-based heuristic. The complexity hint could be an input to the prompt that the LLM overrides if it disagrees, or the LLM could determine it entirely.

---

## Missing Principles from `how.md`

### 5. Extended thinking is excluded (Non-Goal)

`how.md` explicitly states: "Extended thinking mode... can serve as a controllable scratchpad. The lead agent uses thinking to plan its approach... Our testing showed that extended thinking improved instruction-following, reasoning, and efficiency."

The PRD lists extended thinking as a Non-Goal. This is a valid scope decision, but it removes one of the mechanisms Anthropic credits with improving agent quality. The PRD should acknowledge this tradeoff.

### 6. No "let agents improve themselves" mechanism

`how.md` describes a meta-learning loop: "When given a prompt and a failure mode, [Claude models] are able to diagnose why the agent is failing and suggest improvements." They even created a "tool-testing agent" that rewrites tool descriptions.

The PRD has no equivalent. There is no feedback loop where agent failures inform prompt improvements, no mechanism for the system to self-diagnose issues.

### 7. No observability or tracing infrastructure

`how.md` emphasizes: "Adding full production tracing let us diagnose why agents failed and fix issues systematically. Beyond standard observability, we monitor agent decision patterns and interaction structures."

The PRD logs token counts and wall-clock time to `cost_estimate_json` (task 4.5), and writes SubAgentResult artifacts (task 4.4). But there is no structured tracing of agent decisions -- what queries the lead agent considered, why it chose certain angles, what the subagents' intermediate reasoning was. Without this, debugging agent behavior in production will require reading raw logs.

### 8. No human evaluation workflow

`how.md` states: "Human evaluation catches what automation misses. People testing agents find edge cases that evals miss." They specifically found that "early agents consistently chose SEO-optimized content farms over authoritative but less highly-ranked sources."

The PRD relies entirely on automated evaluation (LLM judge + token-overlap NLI). There is no mechanism for human review, no feedback collection from report consumers, and no plan to use human evaluations to improve the system.

### 9. Error recovery is passive, not adaptive

**What `how.md` says:** "Letting the agent know when a tool is failing and letting it adapt works surprisingly well."

**What the PRD proposes:** Task 3.3 catches exceptions and returns a sentinel `SubAgentResult`. The lead agent never learns a subagent failed -- it just receives fewer results. There is no mechanism for the lead agent to retry with a different strategy, spawn a replacement subagent, or adapt its synthesis approach based on which angles succeeded or failed.

---

## Task-Level Issues

### 10. Synthesis pass uses wrong prompt builder

Task 4.1 says `run_synthesis_pass` should use "`build_subagent_prompt()` adapted for synthesis." Synthesis is fundamentally different from subagent work -- a subagent searches and summarizes a single angle, while synthesis merges multiple angle results into a coherent report. It needs its own prompt template (`SYNTHESIS_SYSTEM` / `SYNTHESIS_USER_TEMPLATE`) and builder function (`build_synthesis_prompt()`).

Tasks 1.3-1.5 define prompts for lead agent, subagent, and LLM judge, but there is no synthesis prompt. Task 1.6 defines builder functions for those three but not for synthesis. This is a gap in the task list.

### 11. Research angle count mismatch

The PRD says "7 curated research queries" and requirement 5 lists 7 angles. The existing `research_pass.py` has 6 `CURATED_QUERY_AREAS`. The 7th angle ("Background and prior work") is described as "(new, replaces current catch-all)" but there is no current catch-all to replace -- there are exactly 6 existing angles. This is a minor inaccuracy in the PRD.

### 12. LLM judge model choice

Task 5.2 uses `anthropic_small_model_id` (Haiku) for the LLM judge to "keep cost low." However, judging factual accuracy, citation correctness, and source quality requires strong reasoning. Anthropic's `how.md` describes the judge as "a single LLM call" but doesn't specify using the cheapest available model. Using Haiku for evaluation may produce lower-quality scores that don't reliably catch issues. Consider using Sonnet for the judge, or at least benchmark Haiku judge quality against Sonnet before committing to the cheaper model.

### 13. Duplicate-work guard uses token overlap instead of semantic comparison

Task 2.5 rejects task descriptions with ">60% token overlap." This bag-of-words metric would miss semantically identical tasks phrased differently. Anthropic's approach addresses this through better prompting ("detailed task descriptions" and "clearly divided responsibilities"), not post-hoc string matching. If a duplicate guard is wanted, embedding similarity would be more reliable than token overlap.

### 14. Subagent count ceiling is too low

PRD requirement 2b: 5-7 subagents for complex topics. `how.md`: "complex research might use more than 10 subagents." With only 7 fixed angles, the PRD can't exceed 7 subagents. This is a natural consequence of the fixed-angle constraint (gap #3).

### 15. Dead code: `draft_pass.py`

The tasks file notes `draft_pass.py` is "kept but no longer called by pipeline.py." If it's not called, it should be removed or explicitly marked for deletion. Keeping dead code creates confusion about which code path is active.

### 16. `tool_efficiency` criterion doesn't apply

The LLM judge rubric includes "tool efficiency" (task 1.5, requirement 8b). In Anthropic's system, this measures whether agents used the right tools a reasonable number of times. In this system, subagents make exactly one retrieval call and one LLM call -- there is no tool selection or varying tool usage to evaluate. This criterion is meaningless in the current architecture and should be replaced with something relevant, such as "angle coverage" or "source diversity."

---

## Acknowledged Tradeoffs

The PRD makes several explicit scope decisions that diverge from `how.md`. These are reasonable for a first iteration but should be documented as known simplifications:

- **ThreadPoolExecutor over asyncio** -- acceptable; concurrency model doesn't affect agent quality
- **No subagent-to-subagent communication** -- matches Anthropic's current approach
- **No rainbow deployments** -- reasonable for a CI pipeline vs. a production service
- **No MCP tool integration** -- the existing hybrid retrieval is the only "tool" needed

---

## Recommendations

1. **Redesign subagent execution as a multi-turn search loop** (closes gaps #1, #2). This is the highest-impact change. Without it, the "multi-agent" label is primarily a parallelization wrapper around the existing sequential approach.

2. **Use the lead agent LLM call to assess complexity** (closes gap #4). Pass the heuristic as a hint, let Claude override it.

3. **Add a dedicated synthesis prompt** (closes #10). Don't reuse the subagent prompt for a fundamentally different task.

4. **Replace `tool_efficiency` with a relevant evaluation criterion** (closes #16).

5. **Consider extended thinking for the lead agent planning step** (partially closes #5). Even if subagents don't use it, the planning step benefits most from structured reasoning.

6. **Add structured tracing** (closes #7). At minimum, log the lead agent's full response (including its task decomposition reasoning) as an artifact alongside the SubAgentResult JSONs.

7. **Plan a human evaluation checkpoint** (closes #8). Even a simple manual review of the first N reports with a feedback form would surface issues that automated evaluation misses.
