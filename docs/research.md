# Building an autonomous LLM report pipeline in 2026: primary-source research compendium

This report provides verified, primary-source technical specifications, pricing, and architectural guidance for every component of an autonomous LLM-powered weekly report generation pipeline. **Claude Opus 4.6** (released February 5, 2026)  now offers 128K max output tokens with adaptive thinking and a 1M-token context beta, making deep-research-quality autonomous reports feasible at approximately **$6.50–$18/month** total infrastructure cost. Every figure below is sourced from official documentation unless explicitly flagged otherwise.

-----

## 1. Claude API specifications and pricing as of February 2026

### Claude Opus 4.6

Released **February 5, 2026**,  Opus 4.6 is Anthropic’s current flagship.  Pricing remains at **$5/$25 per million input/output tokens**,  unchanged from Opus 4.5.  The API model ID is `claude-opus-4-6`.  

The max output token limit has doubled to **128K tokens**  (up from 64K), requiring streaming for large `max_tokens` values to avoid HTTP timeouts.   The default context window is **200K tokens**, with a **1M-token beta**  available   for organizations in usage tier 4 or with custom rate limits. When prompts exceed 200K tokens, all tokens incur premium rates: **$10/MTok input** (2×) and **$37.50/MTok output** (1.5×). 

**Adaptive thinking** replaces the old manual `budget_tokens` approach (now deprecated on Opus 4.6). Set via `thinking: {type: "adaptive"}`, Claude dynamically decides when and how deeply to engage extended thinking based on task complexity.  Four effort levels are available: low, medium, **high** (default), and max  — the last being new to Opus 4.6 for peak capability. Thinking tokens are billed as output tokens at standard rates.  Interleaved thinking is automatic with no beta header required.  

Additional new features include context compaction (beta)  for infinite conversations, a Fast Mode research preview at **6× pricing** ($30/$150 per MTok, header `fast-mode-2026-02-01`),   and US-only inference at 1.1× pricing via the `inference_geo` parameter.   A breaking change: assistant message prefilling now returns a 400 error.  

### Claude Sonnet 4.5

Sonnet 4.5 prices at **$3/$15 per million input/output tokens**   with a 200K default context window and the same **1M-token beta** (premium pricing at $6/$22.50 per MTok when exceeding 200K).   Max output is **64K tokens**. Cache pricing is $3.75/MTok for 5-minute writes, $6/MTok for 1-hour writes, and **$0.30/MTok for cache hits**. 

### Batch API

The Batch API provides a flat **50% discount** on both input and output tokens across all models.   Batch Opus pricing drops to $2.50/$12.50 per MTok; Batch Sonnet 4.5 to $1.50/$7.50.  The maximum processing window is **24 hours** (set by the `expires_at` field), with typical turnaround **under 1 hour**.  Each batch supports up to **10,000 queries**.  Batch and prompt caching discounts stack — you can combine both for maximum savings, though cache hit rates in batch mode range from 30–98% due to async processing.  Using the 1-hour cache TTL improves hit rates.

### Prompt caching

Mark content with `cache_control` breakpoints; the system automatically finds the longest matching prefix across approximately 20 content block boundaries.  Cache reads cost **0.1× base input price (90% savings)**. The 5-minute TTL write costs 1.25× base input price; the 1-hour TTL (beta) costs 2× base input price.   Cache is refreshed at no extra cost on each use.  Important caveats: changes to thinking parameters invalidate cached message prefixes (though cached system prompts and tool definitions persist),  and cached input tokens generally do not count toward ITPM rate limits, effectively increasing throughput.  Cache prefixes are created in this order: tools → system → messages. 

### Web search tool

Priced at **$10 per 1,000 searches ($0.01/search)** plus standard token costs for search-generated content.   Tool type is `web_search_20250305`, configurable via `max_uses` to limit searches per request. Claude autonomously decides when to search.   Domain allowlists/blocklists are supported,  and citations are always enabled for search results. Failed searches are not billed.  

Anthropic has **not officially named the search provider**. However, strong circumstantial evidence confirms it is **Brave Search**: Anthropic added “Brave Search” to its subprocessor list, the API contains a `BraveSearchParams` parameter,  and independent testing found 86.7% overlap between Claude’s search results and Brave’s top results.  This was reported by TechCrunch in March 2025 but never formally announced by Anthropic.

### Web fetch tool

Available at **no additional cost** beyond standard token costs for fetched content.   Requires beta header **`web-fetch-2025-09-10`**  (pass as `anthropic-beta: web-fetch-2025-09-10` or via SDK `betas=["web-fetch-2025-09-10"]`). Tool type is `web_fetch_20250910`. A `max_content_tokens` parameter controls content size.  Claude can only fetch URLs explicitly provided by the user or obtained from previous search/fetch results.  Typical token usage: ~2,500 tokens for a 10KB page,  ~25,000 for a 100KB documentation page, ~125,000 for a 500KB PDF. 

-----

## 2. Anthropic’s multi-agent research architecture

Anthropic published “How we built our multi-agent research system” on June 13, 2025, authored by Jeremy Hadfield, Barry Zhang, Kenneth Lien, Florian Scholz, Jeremy Fox, and Daniel Ford.  The post describes an orchestrator-worker pattern with three agent types. 

### The numbers that matter

The multi-agent system with Claude Opus 4 as lead and Sonnet 4 subagents **outperformed single-agent Claude Opus 4 by 90.2%** on Anthropic’s internal research eval.  On the BrowseComp benchmark, **token usage alone explains 80% of performance variance**, with tool calls and model choice as the two other explanatory factors — together explaining 95% of total variance.  Agents use approximately **4× more tokens than chat** interactions, and multi-agent systems use approximately **15× more than chat**   (making multi-agent roughly 3.75× single-agent). A companion blog post from January 2026 cites a consistent “3–10× more tokens” for multi-agent vs. single-agent.  The system cut research time by **up to 90%** for complex queries.  

### Architecture and delegation

The **LeadResearcher** (Opus) analyzes queries, develops strategy, saves plans to Memory (critical because context windows truncate at 200K tokens), spawns variable numbers of subagents, synthesizes results, and iteratively creates more subagents if needed.  **Subagents** (Sonnet) each operate with independent context windows, perform web searches, evaluate results using interleaved thinking, and return compressed findings.   A **CitationAgent** processes the final report to ensure all claims are properly attributed. 

Two levels of parallelization drive speed: the lead agent spawns **3–5 subagents in parallel**, and each subagent uses **3+ tools in parallel**.  Scaling rules embedded in prompts guide effort: simple fact-finding gets 1 agent with 3–10 tool calls; direct comparisons get 2–4 subagents with 10–15 calls each; complex research gets 10+ subagents with clearly divided responsibilities.  Subagents follow an OODA loop (observe → orient → decide → act)  and store work in external systems, passing lightweight references back to the coordinator rather than pushing everything through the lead agent’s context. 

-----

## 3. Railway vs Supabase for a weekly autonomous pipeline

### Railway pricing and capabilities

Railway operates on **pure usage-based pricing** after a base subscription. The Hobby tier costs **$5/month**   (includes $5 usage credit);  Pro is **$20/month**  (includes $20 credit).  The free trial provides a one-time $5 credit  for 30 days,  then converts to $1/month — this is not a permanent free tier.  Resource rates: ~$10/GB/month RAM, ~$20/vCPU/month,  ~$0.16/GB/month volume storage.

Railway supports **one-click PostgreSQL deployment**  with managed backups.  For pgvector, dedicated templates (pg16–pg18) come with the extension pre-installed;  the base Postgres template does not reliably include pgvector binaries.   Native cron support triggers services on a schedule (minimum 5-minute intervals, UTC-based),  with services sleeping between runs — **zero cost when idle**. GitHub integration is first-class with auto-deploy on push and per-PR preview environments.

### Supabase pricing and capabilities

Supabase offers a **permanent free tier** ($0/month) with 500 MB database, 1 GB file storage, 5 GB bandwidth, and 50K MAUs — but projects **pause after 7 days of inactivity**,  which is problematic for weekly pipelines. The Pro tier at **$25/month** provides 8 GB database storage, 100 GB file storage, and daily backups.  

Supabase’s pgvector support is **built-in and first-class**: enable via `CREATE EXTENSION vector` in the dashboard.  It includes extensive AI/vector documentation, Python client libraries, and integrations with OpenAI, Hugging Face, LangChain, and LlamaIndex.  Critically, Supabase provides **official hybrid search with RRF** (Reciprocal Rank Fusion) combining `tsvector` keyword search with `pgvector` semantic search, including a ready-to-use SQL function template with tunable `rrf_k` smoothing.  Scheduling uses `pg_cron` + `pg_net` to trigger Edge Functions,  though Edge Functions have a **2-second CPU time limit**  that is insufficient for heavy LLM processing.

### Which to choose

For a weekly LLM pipeline, the optimal architecture is **hybrid**: Supabase for the database layer (superior pgvector support, built-in hybrid search/RRF, excellent management UI)  and Railway for pipeline execution (full container runtime without CPU limits, pay-per-use cron that costs nothing when idle,  first-class GitHub deployment). Railway alone at ~$5/month is the cheapest single-platform option but requires more manual vector DB setup. Supabase alone at $25/month offers the best developer experience for embeddings but its Edge Function CPU limits constrain heavy LLM processing.

-----

## 4. TranscriptAPI endpoints and pricing

TranscriptAPI (transcriptapi.com) is an active SaaS processing **15M+ transcripts/month**   with a base URL of `https://transcriptapi.com/api/v2`. 

### Endpoints

The channel monitoring endpoint is **`/youtube/channel/latest`** (not `/channel/latest` — all endpoints sit under the `/youtube/` prefix).  Two endpoints are **free** (0 credits but require auth): `/youtube/channel/resolve` (resolves @handle to UC… channel ID) and `/youtube/channel/latest` (latest 15 videos via RSS).   All other endpoints cost **1 credit per successful request**: `/youtube/transcript`, `/youtube/search`, `/youtube/channel/search`, `/youtube/channel/videos` (1 credit/page), and `/youtube/playlist/videos` (1 credit/page).  Credits are only deducted on HTTP 200 responses; failed requests cost nothing. 

### Pricing

The free plan provides 100 one-time credits.   The monthly plan is **$5/month** for 1,000 credits with top-ups at **$2.50 per 1,000 credits**.  The annual plan is **$4.50/month** ($54/year) for 1,000 credits/month  — a 10% discount. Rate limits are 60 RPM on free and 200 RPM on paid plans. 

### MCP integration

TranscriptAPI implements full MCP at `https://transcriptapi.com/mcp`,  exposing 6 tools: `get_youtube_transcript`, `search_youtube`, `get_channel_latest_videos` (free), `search_channel_videos`, `list_channel_videos`, and `list_playlist_videos`.  For Claude, it uses OAuth  2.1 with Dynamic Client Registration — no credentials needed, just add the MCP server URL. For OpenAI Agent Builder, it uses API key authentication.   Supported platforms include Claude, ChatGPT (Developer Mode), and any MCP-compatible client. 

-----

## 5. DeepSeek V3.2 specifications

Released December 1, 2025, DeepSeek V3.2 uses the model names **`deepseek-chat`** (non-thinking mode) and **`deepseek-reasoner`** (thinking mode) in the API. The context window is **128K tokens**   (not 64K). Max output for `deepseek-chat` is **8K tokens** (default 4K); for `deepseek-reasoner` it is **64K tokens** (default 32K). 

Pricing (per million tokens, both models): **$0.028 input (cache hit)**, **$0.28 input (cache miss)**, **$0.42 output**.  Context caching is automatic and enabled by default.  Off-peak discounts were eliminated after September 5, 2025.  At these prices, DeepSeek V3.2 is roughly **10× cheaper** than Claude Sonnet 4.5 on cache-miss input and **36× cheaper** on output. 

-----

## 6. GPT-5.2 — OpenAI’s current flagship

Released December 10, 2025, GPT-5.2 is OpenAI’s latest flagship model for “coding and agentic tasks.”  It features a **400,000-token context window**  and **128,000-token max output**. Pricing is **$1.75 input / $14.00 output per million tokens**,   with cached input at $0.175/MTok. Reasoning support is included with configurable effort levels (none/low/medium/high/xhigh).  Knowledge cutoff is August 31, 2025. 

A GPT-5.2 Pro variant is available at $21/$168 per MTok via the Responses API only.  The broader model family includes GPT-5 ($1.25/$10),  GPT-5 mini ($0.25/$2), and GPT-5 nano ($0.05/$0.40).  OpenAI’s Batch API provides a 50% discount.  

-----

## 7. Gemini 2.5 Pro specifications

Gemini 2.5 Pro offers the largest context window of any major model at **1,048,576 tokens (1M)**  with **65,536 max output tokens**. The model code is `gemini-2.5-pro`.  Pricing for prompts ≤200K tokens: **$1.25 input / $10.00 output per MTok**.  For prompts exceeding 200K tokens: $2.50 input / $15.00 output.  Context caching drops input to $0.125/MTok (≤200K). Batch API provides 50% off.  A free tier is available with rate limits. 

Since mid-2025, Gemini 2.5 Pro has transitioned from preview to **stable**, pricing dropped from the preview’s $2.00/$12.00 to the current $1.25/$10.00, and tiered pricing for >200K token prompts was introduced.  Google has also released **Gemini 3 Pro Preview** ($2.00/$12.00) and Gemini 3 Flash Preview ($0.50/$3.00),  though Gemini 2.5 Pro remains the stable production model.

-----

## 8. OpenAI text-embedding-3-small — confirmed specs

**Confirmed**: 1,536 default dimensions  (supports dimension reduction via the `dimensions` API parameter), 8,191 max input tokens, and pricing of **$0.02 per million tokens** ($0.01 for batch).  This is 5× cheaper than the legacy text-embedding-ada-002.  Only input tokens are charged.  For reference, text-embedding-3-large offers 3,072 dimensions  at $0.13/MTok. 

-----

## 9. Citation accuracy and verification architectures

### What the research actually shows

The **~74% citation figure** comes from Mugaanyi et al. (JMIR, April 2024), who evaluated 102 GPT-3.5-generated citations: **72.7% in natural sciences and 76.6% in humanities were confirmed to exist**  — but DOI accuracy was far lower (32.7% and 8.5% respectively).  A larger study, GhostCite (February 2025), benchmarked 13 LLMs across 40 domains and found hallucination rates ranging from **14.23% to 94.93%**, with only 49.71% of 331,809 citations verified as valid.  The Stanford SourceCheckup study (Nature Communications, April 2025) found that even GPT-4o with Web Search leaves ~30% of individual statements unsupported.  

The **~80% misattribution claim** is directionally correct but cannot be traced to a single definitive paper. Multiple sources confirm that misattribution (correct title with wrong authors, or correct authors with wrong venue) is the dominant error type rather than complete fabrication. The GhostCite study and the FACTUM paper (January 2025) both characterize this pattern, and SPY Lab’s analysis of arXiv hallucinated references found most involved correct titles with wrong authors.  **⚠️ Flag: the exact 80% figure should be independently verified.**

### How to build a citation-first generation pipeline

The strongest pattern combines **source-aware generation** with **post-hoc verification**. During generation, pass numbered source chunks in context and instruct the model to cite chunk IDs inline (“Based on [Source 3], revenue grew…”). This is the approach used by Cohere’s native citation API and LlamaIndex’s `CitationQueryEngine`. 

For verification against stored source material only (no external search), the recommended architecture is:

1. **Claim extraction**: Decompose generated text into atomic facts (following the FActScore methodology from Min et al., EMNLP 2023), tagging each with its cited source ID(s).
1. **NLI verification**: For each claim-source pair, classify as Supported / Contradicted / Not Enough Info. MiniCheck (Tang et al., 2024) achieves **GPT-4-level fact-checking at 400× lower cost** by training small models on synthetic challenging errors. 
1. **Scoring and flagging**: “Contradicted” → flag for removal; “Not Enough Info” → flag as potentially unsupported; no citation → flag as ungrounded. Compute an overall grounding score as the percentage of supported claims.
1. **Revision**: Use RARR-style editing (Gao et al., ACL 2023) to revise unsupported claims, or Self-Refine loops (Madaan et al., ICLR 2024, showing ~20% absolute improvement) where the same LLM provides feedback and iterates. 

The SAFE framework (Google DeepMind, March 2024) provides the most rigorous evaluation approach, agreeing with human annotators 72% of the time and winning 76% of disagreement cases  at **20× lower cost** than human annotation ($0.19 vs $4.00 per response).  For production pipelines, Self-RAG (Asai et al., ICLR 2024 Oral) trained with reflection tokens achieves **80% factuality on biography generation** vs. 71% for ChatGPT, with significantly higher citation precision.  

-----

## 10. Open-source report generation frameworks

### GPT-Researcher

At 25.3K GitHub stars (Apache 2.0),  GPT-Researcher is the most established dedicated research agent.  **The $0.005/report figure does not appear anywhere in official sources.** Their FAQ claims **~$0.01** (for GPT-4), while their introduction page claims **~$0.1** (for the current gpt-4o-mini + gpt-4o architecture). These are 10× apart, reflecting different model configurations and eras. The ~$0.1 figure from the introduction page appears most current. The architecture uses a planner-execution pattern: a planner agent generates research questions, execution agents crawl and gather information in parallel, and a publisher aggregates findings.   Version 3.3.0 (June 2025) added MCP support.  Carnegie Mellon’s DeepResearchGym ranked it highest among Perplexity, OpenAI, OpenDeepSearch, and HuggingFace. 

### LangChain Open Deep Research

At 10.3K stars (MIT), this is the most actively developed framework with 195 commits  through August 2025. It achieved **#6 on the Deep Research Bench leaderboard** (RACE score 0.4344).  The architecture has three phases: scoping (user clarification + brief generation), research (single-agent or multi-agent supervisor spawning parallel sub-agents), and report writing (single one-shot generation — their experiments showed parallel writing degraded quality).  Built on LangGraph, it supports any LLM via `init_chat_model()` and multiple search backends including Tavily, OpenAI Native Web Search, and Anthropic Native Web Search. 

### CrewAI

The most-starred at 41.2K (MIT),  CrewAI is a **general-purpose multi-agent framework**, not a purpose-built research tool.  It offers three paradigms: Crews (role-based agent teams), Flows (event-driven workflows),  and Pipelines (multi-stage workflows).  The quickstart template demonstrates a researcher + reporting analyst pattern producing markdown reports.   It requires manual pipeline construction but supports MCP, multiple LLM providers, and enterprise deployment.  Updated January 2026. 

### STORM (Stanford)

At 27.9K stars (MIT), STORM generates Wikipedia-style articles using a perspective-guided simulated conversation between a “Wikipedia writer” and a “topic expert.” Built on DSPy, it uses LiteLLM for model flexibility (v1.1.0, January 2025). Co-STORM adds human-AI collaborative curation (EMNLP 2024).  Development pace is slower than competitors — the last release was 13+ months ago — reflecting its academic origins.

-----

## 11. GitHub Actions and infrastructure architecture

### GitHub Actions specifications

Cron scheduling uses standard 5-field POSIX syntax,  running only on the default branch in **UTC**. Minimum interval is 5 minutes.  **Critical caveat**: cron defines when a job is queued, not when it runs — delays of 15–20 minutes are common, and jobs may be dropped under heavy load.  Scheduled workflows on public repos **auto-disable after 60 days** without a commit. 

Secrets use **Libsodium sealed-box encryption**  with a **48 KB per-secret limit** and caps of 100 repository secrets, 100 environment secrets, and 1,000 organization secrets.  Jobs on GitHub-hosted runners have a **6-hour hard limit** (360 minutes); self-hosted runners have no such limit.  Workflow runs cap at 72 hours total. 

Public repositories get **unlimited free** standard Linux runner minutes.  Private repos on the Free plan get **2,000 minutes/month**   (Linux = 1×, Windows = 2×, macOS = 10× multiplier).  As of January 1, 2026, runner prices were reduced up to 39%.  A planned $0.002/min self-hosted runner charge announced in December 2025  was **rescinded after backlash** and remains under re-evaluation. 

### Complete pipeline architecture

The recommended GitHub Actions workflow structure uses sequential jobs with `needs:` dependency chains and a shared database for state:

**Stage 1 — Ingestion** (RSS via `feedparser`, YouTube via TranscriptAPI or `youtube-transcript-api`): Fetch feeds → deduplicate against database by URL/GUID → store new entries. Error handling: HTTP retry with exponential backoff, 30s timeout per feed, continue on individual failures.

**Stage 2 — Embedding** (OpenAI `text-embedding-3-small`): Chunk documents using `RecursiveCharacterTextSplitter` (512–1000 tokens, 50–100 overlap) → batch-generate embeddings → upsert into pgvector with metadata. Idempotent upserts using source_id + chunk_index.

**Stage 3 — Multi-pass generation**: (1) Research pass queries pgvector for relevant chunks using curated queries; (2) Draft pass with Claude Sonnet 4.5 generates the report using assembled context; (3) Critique pass with a separate Claude call (or cheaper Haiku 4.5) reviews for accuracy and hallucinations against source material; (4) Revision pass incorporates critique. Use prompt caching for the system prompt across all calls (90% savings).

**Stage 4 — Delivery**: Commit to GitHub repo (auto-publish via Pages), email via SendGrid/SES, or post to Slack webhook.

### Monthly cost breakdown

|Component                             |Low estimate  |High estimate|
|--------------------------------------|--------------|-------------|
|GitHub Actions (public repo)          |$0            |$0           |
|Claude API (Sonnet 4.5, 4 passes/week)|$1.50         |$10.00       |
|OpenAI embeddings (50–100 docs/week)  |$0.01         |$0.05        |
|Railway Hobby (pgvector DB)           |$5.00         |$8.00        |
|TranscriptAPI (monthly plan)          |$5.00         |$5.00        |
|**Total**                             |**~$11.50/mo**|**~$23/mo**  |

Cost can be driven lower with Batch API (50% off Claude), prompt caching, using Haiku 4.5 for the critique pass ($1/$5 per MTok), and Supabase free tier instead of Railway (eliminating the $5–8 DB cost, though requiring workarounds for the 7-day inactivity pause). 

-----

## Conclusion

The 2026 LLM ecosystem makes autonomous deep-research pipelines remarkably accessible. Claude Opus 4.6’s 128K output and adaptive thinking,  combined with 50% batch discounts  and 90% cache savings,  bring per-report API costs to under $3 even for multi-pass architectures. The critical architectural insight from Anthropic’s own research is that **token budget explains 80% of performance variance**   — meaning pipeline design should optimize for generous context windows and multiple passes over raw model selection.

Three verification findings should shape pipeline design: citation existence rates hover around 50–75% across models,  misattribution dominates over fabrication as the primary error type, and MiniCheck-style small models can perform GPT-4-level fact-checking at 400× lower cost.  The most robust architecture combines citation-first generation (inline source IDs during drafting) with NLI-based post-hoc verification against stored chunks only — avoiding the circular problem of using web search to “verify” LLM outputs.

For infrastructure, the hybrid Supabase (database with built-in pgvector + RRF hybrid search)  plus Railway (cron-triggered container execution,  zero cost when idle) approach balances developer experience with cost. GitHub Actions provides free orchestration for public repos with the important caveat that cron reliability requires a `workflow_dispatch` fallback. The entire stack — ingestion, embedding, multi-pass generation, verification, and delivery — runs for **$12–23/month** at weekly cadence, a figure that would have been implausible even 18 months ago.