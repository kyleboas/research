# Implementation plan: autonomous weekly LLM research report pipeline

This plan translates `docs/research.md` into an actionable implementation roadmap, including the proposed hybrid infrastructure (Supabase + Railway + GitHub Actions).

## 1) Goals and non-goals

### Goals
- Build a weekly, end-to-end automated research report system.
- Ingest source material from RSS and YouTube channels/transcripts.
- Store raw items + embeddings in a vector-enabled database.
- Generate reports with a multi-pass LLM pipeline (research, draft, critique, revise).
- Enforce citation-grounded output and post-hoc factual verification.
- Publish outputs to the repository and optionally distribute to email/Slack.
- Keep monthly operating cost in the target range from research (~$12–23/month baseline).

### Non-goals (phase 1)
- Real-time streaming dashboards.
- Human-in-the-loop editing UI (beyond markdown review in GitHub).
- Multi-tenant user management.

---

## 2) Proposed infrastructure (recommended)

## Core architecture
- **Orchestration:** GitHub Actions (scheduled + manual fallback).
- **Execution runtime:** Railway (containerized worker jobs, cron-capable, zero cost while idle).
- **Database and retrieval:** Supabase Postgres + pgvector + hybrid search (RRF).
- **LLM generation:** Anthropic Claude Sonnet 4.5 (default), optional Haiku 4.5 for critique cost control.
- **Embeddings:** OpenAI `text-embedding-3-small`.
- **Transcript ingestion:** TranscriptAPI (`/youtube/channel/latest` + transcript/search endpoints).
- **Delivery:** GitHub Pages (repo commit), optional SendGrid/SES + Slack webhook.

## Why this split
- Supabase gives the best vector DB developer experience and built-in hybrid retrieval.
- Railway avoids Edge Function CPU constraints for heavy LLM pipelines.
- GitHub Actions provides free/low-cost orchestration and native repo integration.

## Environment layout
- **GitHub Secrets:** API keys and connection strings.
- **Railway variables:** runtime secrets mirrored from GitHub/managed per environment.
- **Supabase project:** `public` schema for app tables; vector extension enabled.

---

## 3) Repository and code structure (target)

```text
/docs/
  research.md
  plan.md
/src/
  config.py
  pipeline.py
  ingestion/
    rss.py
    youtube.py
    dedupe.py
  processing/
    chunking.py
    embeddings.py
    retrieval.py
  generation/
    prompts.py
    research_pass.py
    draft_pass.py
    critique_pass.py
    revision_pass.py
  verification/
    claims.py
    nli_check.py
    scoring.py
  delivery/
    github_publish.py
    email.py
    slack.py
/sql/
  001_init.sql
  002_vector_indexes.sql
  003_hybrid_search.sql
/.github/workflows/
  weekly_report.yml
```

(Exact language/framework may be adjusted, but modules should follow these responsibilities.)

---

## 4) Data model and storage plan

## Tables
- `sources`
  - `id`, `source_type` (`rss|youtube`), `source_key`, `title`, `url`, `published_at`, `raw_text`, `meta_json`, `ingested_at`.
  - Unique index on (`source_type`, `source_key`) and fallback unique on normalized URL.
- `chunks`
  - `id`, `source_id`, `chunk_index`, `chunk_text`, `token_count`, `created_at`.
  - Unique index (`source_id`, `chunk_index`).
- `embeddings`
  - `chunk_id`, `embedding vector(1536)`, `model`, `created_at`.
- `reports`
  - `id`, `week_of`, `status`, `draft_markdown`, `final_markdown`, `metrics_json`, `created_at`, `updated_at`.
- `claims`
  - `id`, `report_id`, `claim_text`, `cited_chunk_ids`, `verification_status`, `confidence`, `notes`.
- `pipeline_runs`
  - `id`, `trigger_type`, `started_at`, `ended_at`, `status`, `error_summary`, `cost_estimate_json`.

## Retrieval
- pgvector ANN index on `embeddings.embedding`.
- Full-text index on chunk/source text (`tsvector`).
- SQL function for **hybrid RRF** ranking (semantic + keyword).

---

## 5) End-to-end pipeline design

## Stage 1 — Ingestion
1. Load configured RSS feeds + YouTube channels.
2. RSS fetch with retry/backoff and per-feed timeout.
3. YouTube monitoring via TranscriptAPI `/youtube/channel/latest`.
4. For new videos/articles only, fetch transcript/body.
5. Normalize and persist records in `sources`.

**Idempotency:** dedupe by URL/GUID/channel video ID.

## Stage 2 — Chunking + embeddings
1. Chunk source text (target 512–1000 tokens, overlap 50–100).
2. Insert chunks with deterministic `chunk_index`.
3. Batch embed via `text-embedding-3-small`.
4. Upsert vectors into `embeddings`.

**Idempotency:** upsert by (`source_id`, `chunk_index`) and `chunk_id`.

## Stage 3 — Multi-pass generation
1. **Research pass:** run curated retrieval queries against hybrid search.
2. **Draft pass:** Sonnet 4.5 produces initial report with inline source IDs.
3. **Critique pass:** separate model call reviews grounding/hallucinations.
4. **Revision pass:** integrate critique and produce final report.

**Optimization:** prompt caching for stable system prompt/tool definitions.

## Stage 4 — Verification
1. Extract atomic claims from final draft.
2. Verify each claim against cited chunks only (Supported/Contradicted/NEI).
3. Compute grounding score and list unsupported claims.
4. Optional auto-revision loop if score below threshold.

## Stage 5 — Delivery
1. Write markdown to `/reports/YYYY-MM-DD-weekly-report.md`.
2. Commit artifact to repo (for Pages publication).
3. Send optional email digest and/or Slack notification.

---

## 6) Model and tool selection policy

## Default model routing
- Research + Draft + Revision: **Claude Sonnet 4.5**.
- Critique: **Haiku 4.5** (default for cost) or Sonnet (quality mode).
- Optional deep-research escalation: Opus 4.6 for selected weeks/topics.

## Tool usage
- Web search only when internal sources are insufficient.
- Web fetch for specific referenced URLs.
- Enforce max tool-call/search limits per run.

## Prompting requirements
- Citation-first instruction: every factual claim must include source chunk ID.
- Structured output schema for critique and verification handoff.

---

## 7) GitHub Actions workflow plan

## Workflow file: `.github/workflows/weekly_report.yml`
- Triggers:
  - `schedule` (weekly cron, UTC).
  - `workflow_dispatch` (manual fallback due cron delays/drops).
- Jobs (with `needs`):
  1. `ingest`
  2. `embed`
  3. `generate`
  4. `verify`
  5. `deliver`
- Concurrency guard to prevent overlapping weekly runs.
- Artifacts: logs, run summary, verification report.

## Runtime options
- Option A: Execute directly on GitHub-hosted runner.
- Option B (recommended): Workflow triggers Railway job/container for heavy steps.

---

## 8) Railway deployment plan

1. Create Railway project with worker service.
2. Dockerize pipeline application.
3. Configure scheduled trigger (weekly), plus HTTP/manual trigger.
4. Inject secrets and DB URL.
5. Enable observability (structured logs, run duration, failure alerts).

**Reasoning:** long-running LLM operations and retries fit Railway better than constrained edge runtimes.

---

## 9) Supabase setup plan

1. Create project and enable `vector` extension.
2. Apply SQL migrations for tables/indexes/functions.
3. Implement RRF hybrid search SQL function.
4. Configure Row Level Security according to service-role-only writes.
5. Add backups and retention policy.

---

## 10) Configuration and secret management

## Required secrets
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `TRANSCRIPTAPI_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `DATABASE_URL` (if direct Postgres connection used)
- `SLACK_WEBHOOK_URL` (optional)
- `SENDGRID_API_KEY` or SES credentials (optional)

## Config file
- Feed/channel lists.
- Retrieval query templates.
- Token and cost limits.
- Verification thresholds (e.g., minimum grounding score).

---

## 11) Reliability, observability, and quality gates

## Reliability controls
- Exponential backoff + timeout for all external calls.
- Partial-failure tolerance on ingestion.
- Retry strategy with capped attempts for LLM and embedding calls.
- Idempotent database writes across all stages.

## Observability
- Structured JSON logs per stage.
- Run metrics: docs ingested, chunks embedded, tokens used, estimated cost, report length, grounding score.
- Error categorization and alert routing.

## Quality gates before publish
- Minimum citation density (e.g., >=1 citation per factual paragraph).
- No unresolved `Contradicted` claims.
- Grounding score above threshold (e.g., >=0.85).

---

## 12) Security and compliance

- Keep all secrets in GitHub/Railway/Supabase secret stores (never in repo).
- Use least-privilege service tokens.
- Sanitize/escape fetched content before prompt inclusion.
- Maintain source URL provenance in report metadata.

---

## 13) Cost controls

- Use Sonnet/Haiku defaults; reserve Opus for escalations.
- Batch embeddings and optional batch generation where latency permits.
- Prompt caching on static sections (system prompt/tools).
- Max weekly input budget and hard fail-safe when exceeded.

**Expected baseline:** approximately low teens to low twenties USD/month at weekly cadence, with optimization headroom via caching and batch.

---

## 14) Implementation phases and milestones

## Phase 0 (Day 1–2): Foundations
- Project skeleton + config loader.
- Supabase schema + migrations.
- Basic ingest pipeline (RSS + channel latest).

## Phase 1 (Day 3–5): Retrieval core
- Chunking + embeddings + vector upserts.
- Hybrid retrieval function and integration tests.

## Phase 2 (Day 6–8): Generation pipeline
- Multi-pass prompts and orchestration.
- Markdown report output with inline source IDs.

## Phase 3 (Day 9–10): Verification
- Claim extraction + NLI classification + grounding score.
- Publish gating rules.

## Phase 4 (Day 11–12): Automation + delivery
- GitHub Action workflow and Railway triggers.
- GitHub commit delivery + Slack/email hooks.

## Phase 5 (Day 13+): Hardening
- Load/perf tests, failure injection, cost tuning.
- Optional multi-agent parallel subagent research mode.

---

## 15) Definition of done

- Weekly run executes unattended from trigger to published report.
- New content ingestion is deduplicated and persisted.
- Report includes inline source references tied to stored chunks.
- Verification stage runs and enforces quality threshold.
- Pipeline emits run metrics and estimated cost.
- Recovery path documented for failed runs (rerun by stage).

---

## 16) Immediate next actions

1. Confirm runtime language (Python recommended for ecosystem fit).
2. Create SQL migrations and provision Supabase.
3. Implement Stage 1 ingestion module with fixtures.
4. Implement chunking/embedding/upsert flow.
5. Stand up first end-to-end dry run on a small source set.
