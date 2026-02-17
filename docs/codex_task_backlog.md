# Codex task backlog: autonomous weekly LLM research report pipeline

This backlog is designed to be copied into issues or used as Codex prompts directly.
Each ticket includes **exact file-level acceptance criteria** so implementation can be verified quickly.

---

## Ticket 1 — Project scaffold and runtime wiring

**Goal**
Create the base folder/module structure and shared runtime config.

**Files to create/update**
- `src/config.py`
- `src/pipeline.py`
- `src/ingestion/__init__.py`
- `src/processing/__init__.py`
- `src/generation/__init__.py`
- `src/verification/__init__.py`
- `src/delivery/__init__.py`
- `sql/001_init.sql` (placeholder if full schema lands in Ticket 2)

**Acceptance criteria (file-level)**
- `src/config.py`
  - Defines a typed settings object (env-backed) for:
    - Supabase/Postgres connection
    - Anthropic API key/model IDs
    - OpenAI API key/embedding model
    - Transcript API key
    - GitHub token/repo metadata
  - Exposes a single loader function used by pipeline entrypoints.
- `src/pipeline.py`
  - Defines CLI entrypoints or callable functions for each stage:
    - `run_ingestion()`
    - `run_embedding()`
    - `run_generation()`
    - `run_verification()`
    - `run_delivery()`
    - `run_all()`
  - Emits structured logs including `pipeline_run_id` and stage timing.
- `src/*/__init__.py`
  - Present for all listed packages and imports are valid.

---

## Ticket 2 — Database schema, indexes, and hybrid retrieval SQL

**Goal**
Implement normalized schema + pgvector + hybrid RRF primitives.

**Files to create/update**
- `sql/001_init.sql`
- `sql/002_vector_indexes.sql`
- `sql/003_hybrid_search.sql`

**Acceptance criteria (file-level)**
- `sql/001_init.sql`
  - Creates tables: `sources`, `chunks`, `embeddings`, `reports`, `claims`, `pipeline_runs`.
  - Includes constraints and uniqueness for:
    - `sources(source_type, source_key)`
    - normalized URL dedupe strategy
    - `chunks(source_id, chunk_index)`
  - Includes FK relationships and updated timestamp defaults.
- `sql/002_vector_indexes.sql`
  - Enables required extensions (`vector`, text search support as needed).
  - Creates ANN/vector index for embeddings.
  - Creates full-text indexes for chunk/source search.
- `sql/003_hybrid_search.sql`
  - Adds SQL function(s) for hybrid retrieval using RRF-style score fusion.
  - Function returns chunk ID, source ID, combined score, and rank metadata.

---

## Ticket 3 — RSS ingestion with retries and idempotent dedupe

**Goal**
Implement robust RSS fetch/store workflow.

**Files to create/update**
- `src/ingestion/rss.py`
- `src/ingestion/dedupe.py`
- `src/pipeline.py`

**Acceptance criteria (file-level)**
- `src/ingestion/rss.py`
  - Fetches configured feeds with per-feed timeout and retry/backoff.
  - Parses title/url/published/content and normalizes records.
  - Returns deterministic records including stable source keys.
- `src/ingestion/dedupe.py`
  - Provides URL normalization + GUID/source key dedupe helpers.
  - Exposes a function that filters out existing rows by DB lookup before insert.
- `src/pipeline.py`
  - `run_ingestion()` invokes RSS ingestion path and persists only new sources.
  - Logs counts: fetched, deduped, inserted, failed.

---

## Ticket 4 — YouTube channel monitoring and transcript ingestion

**Goal**
Add YouTube source collection via transcript provider and merge into source table.

**Files to create/update**
- `src/ingestion/youtube.py`
- `src/ingestion/dedupe.py`
- `src/pipeline.py`

**Acceptance criteria (file-level)**
- `src/ingestion/youtube.py`
  - Implements latest-video polling per configured channel.
  - Fetches transcript text and metadata for new videos.
  - Handles missing transcript edge cases without failing whole stage.
- `src/ingestion/dedupe.py`
  - Includes channel/video-ID dedupe helper used by YouTube pipeline.
- `src/pipeline.py`
  - `run_ingestion()` merges RSS + YouTube outcomes into unified write path.

---

## Ticket 5 — Chunking pipeline with deterministic indices

**Goal**
Split source text into retrieval-ready chunks.

**Files to create/update**
- `src/processing/chunking.py`
- `src/pipeline.py`

**Acceptance criteria (file-level)**
- `src/processing/chunking.py`
  - Chunks text into configurable window size with overlap.
  - Produces deterministic `chunk_index` ordering.
  - Computes approximate token counts per chunk.
- `src/pipeline.py`
  - `run_embedding()` loads unchunked/new sources and upserts chunks idempotently.
  - Writes stage metrics (sources processed, chunks created/updated).

---

## Ticket 6 — Embedding generation and vector upsert

**Goal**
Generate embeddings and persist vectors for hybrid retrieval.

**Files to create/update**
- `src/processing/embeddings.py`
- `src/processing/retrieval.py`
- `src/pipeline.py`

**Acceptance criteria (file-level)**
- `src/processing/embeddings.py`
  - Batch-calls embedding API with retry/backoff.
  - Supports model override via config.
  - Upserts embedding vectors by `chunk_id`.
- `src/processing/retrieval.py`
  - Exposes retrieval helper calling `sql/003_hybrid_search.sql` function.
  - Returns top-k chunks with source metadata.
- `src/pipeline.py`
  - `run_embedding()` performs chunk -> embed -> upsert flow with resumable behavior.

---

## Ticket 7 — Multi-pass generation prompts and passes

**Goal**
Implement research, draft, critique, and revision stages.

**Files to create/update**
- `src/generation/prompts.py`
- `src/generation/research_pass.py`
- `src/generation/draft_pass.py`
- `src/generation/critique_pass.py`
- `src/generation/revision_pass.py`
- `src/pipeline.py`

**Acceptance criteria (file-level)**
- `src/generation/prompts.py`
  - Centralizes system/user templates with citation format requirements.
  - Contains prompt sections suitable for caching (stable prefix).
- `src/generation/research_pass.py`
  - Executes curated retrieval queries and assembles context packet.
- `src/generation/draft_pass.py`
  - Produces markdown draft with inline source/chunk references.
- `src/generation/critique_pass.py`
  - Evaluates grounding/hallucination risk against retrieved context only.
- `src/generation/revision_pass.py`
  - Produces final markdown incorporating critique deltas.
- `src/pipeline.py`
  - `run_generation()` persists draft/final report artifacts and stage metrics.

---

## Ticket 8 — Claim extraction and factual verification

**Goal**
Add post-hoc claim checking and scoring.

**Files to create/update**
- `src/verification/claims.py`
- `src/verification/nli_check.py`
- `src/verification/scoring.py`
- `src/pipeline.py`

**Acceptance criteria (file-level)**
- `src/verification/claims.py`
  - Extracts atomic claims from final markdown with stable IDs.
- `src/verification/nli_check.py`
  - Checks each claim against cited chunks only; returns supported/unsupported/uncertain.
- `src/verification/scoring.py`
  - Produces report-level quality score and claim summary stats.
- `src/pipeline.py`
  - `run_verification()` stores claim rows + verification status in DB.

---

## Ticket 9 — Delivery integrations (GitHub, email, Slack)

**Goal**
Publish finalized outputs and optional notifications.

**Files to create/update**
- `src/delivery/github_publish.py`
- `src/delivery/email.py`
- `src/delivery/slack.py`
- `src/pipeline.py`

**Acceptance criteria (file-level)**
- `src/delivery/github_publish.py`
  - Commits/updates report markdown in deterministic path convention.
  - Supports dry-run mode.
- `src/delivery/email.py`
  - Sends summary with link/attachment using configured provider.
- `src/delivery/slack.py`
  - Posts summary + report URL to webhook.
- `src/pipeline.py`
  - `run_delivery()` can independently run for an existing finalized report.

---

## Ticket 10 — GitHub Actions workflow and orchestration hardening

**Goal**
Automate weekly runs with manual fallback and stage isolation.

**Files to create/update**
- `.github/workflows/weekly_report.yml`
- `src/pipeline.py`

**Acceptance criteria (file-level)**
- `.github/workflows/weekly_report.yml`
  - Includes both `schedule` and `workflow_dispatch` triggers.
  - Uses sequential jobs/stages with explicit dependencies.
  - Injects secrets via environment variables only.
  - Uploads logs/artifacts for failure triage.
- `src/pipeline.py`
  - Exposes stage-specific CLI args used by workflow steps.

---

## Ticket 11 — Test harness and smoke checks

**Goal**
Guarantee idempotency and baseline reliability.

**Files to create/update**
- `tests/test_ingestion_idempotency.py`
- `tests/test_chunking_determinism.py`
- `tests/test_retrieval_hybrid.py`
- `tests/test_generation_citation_format.py`
- `tests/test_verification_flow.py`

**Acceptance criteria (file-level)**
- `tests/test_ingestion_idempotency.py`
  - Running ingestion twice with same fixtures inserts no duplicates.
- `tests/test_chunking_determinism.py`
  - Same input produces same chunk boundaries and indices.
- `tests/test_retrieval_hybrid.py`
  - Hybrid search returns both semantic and keyword-relevant chunks in top-k.
- `tests/test_generation_citation_format.py`
  - Draft/final report includes required citation markers.
- `tests/test_verification_flow.py`
  - Supported/unsupported claims are persisted with score outputs.

---

## Ticket 12 — Operational runbook and cost telemetry

**Goal**
Make operations repeatable and cost-visible.

**Files to create/update**
- `docs/runbook.md`
- `docs/cost_model.md`
- `src/pipeline.py`

**Acceptance criteria (file-level)**
- `docs/runbook.md`
  - Documents local run, scheduled run, replay, and failure recovery.
- `docs/cost_model.md`
  - Defines per-run and monthly cost formulas by stage and model.
  - Includes “weekly vs multi-weekly” examples and Opus vs Sonnet multiplier.
- `src/pipeline.py`
  - Persists per-stage token and estimated cost metrics into `pipeline_runs.cost_estimate_json`.

---

## Suggested execution order

1. Ticket 1–2 (foundation)
2. Ticket 3–6 (ingestion + retrieval)
3. Ticket 7–8 (generation + verification)
4. Ticket 9–10 (delivery + orchestration)
5. Ticket 11–12 (quality + operations)

This order minimizes integration risk and keeps each PR reviewable.
