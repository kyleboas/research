# Football Research Pipeline

Automated football research workflow that:

1. ingests RSS + YouTube transcript content,
2. detects potentially novel tactical trends, and
3. generates citation-checked markdown reports.

The core pipeline lives in `main.py`, with a lightweight dashboard/runner in `server.py`.

## Agent Quickstart

If you are working in this repo with a coding agent, start here:

- [`AGENTS.md`](AGENTS.md): editing rules, entrypoints, and verification commands
- [`docs/repo-map.md`](docs/repo-map.md): module ownership and change paths
- `make help`: discover the common run/test commands without reconstructing them

The repo is organized around stable surfaces rather than deep package nesting:

- ingest: `main.py` + `article_extractor.py`
- detect: `detect_*.py` + `trend_detection.py` + `novelty_scoring.py`
- report: `main.py`
- dashboard: `server.py` + `dashboard.html`

## Pipeline overview

### 1) Ingest (`--step ingest`)

- Pulls RSS stories directly from the feeds listed in `feeds/rss.md`.
- Pulls recent videos from configured YouTube channels via channel RSS feeds.
- Fetches transcripts from `defuddle.md` for discovered videos.
- Fetches full article text for RSS items via `defuddle.md` first, with local extraction fallbacks in `article_extractor.py`.
- Re-reads a configurable overlap window on incremental runs so late-arriving RSS stories or videos are deduped instead of missed.
- Uses conservative default pacing for feed fetches, Defuddle requests, and embeddings to reduce rate-limit bursts during ingest.
- Chunks content and stores embeddings in Postgres (`source_chunks.embedding`).

### 2) Detect (`--step detect`)

Detection is layered:

- BERTrend-inspired weak-signal detector (`trend_detection.py`).
- Tactical pattern detector (`tactical_extraction.py` + `novelty_scoring.py`).
- LLM-only fallback if algorithmic detectors produce no candidates.

Candidate scores are adjusted by:

- historical user feedback (`trend_feedback`),
- novelty bonus/penalty,
- source diversity.

### 3) Report (`--step report`)

The report stage selects top pending candidates that pass quality gates and then runs a multi-agent research flow:

- planner / lead researcher,
- parallel OODA-loop subagents,
- synthesis,
- sufficiency check (+ optional second round),
- citation verification,
- final revision,
- persistent report artifacts (lead plan, subagent briefs, draft, citation review).

The report stage also reads `report_policy_config.json`, which controls the
number of research rounds, minimum delegated angles, retrieval depth, and
generation token budgets. That gives the report system a small, explicit tuning
surface that can be evaluated without inventing a separate deployment path.

Final reports are saved to:

- Postgres table `reports`, and
- local `_posts/YYYY/MM/YYYY-MM-DD-<slug>.md` in Jekyll post format.

When `GITHUB_TOKEN` and `GITHUB_REPO` are configured, the same `_posts/...` file
is pushed to a dedicated branch, a pull request is opened against
`GITHUB_BRANCH`, and the stored report metadata includes the resulting PR URL.
If `DISCORD_WEBHOOK_URL` is configured, the app also posts the new PR link to
Discord. The same webhook is also used by the Railway dashboard to post alerts
for newly inserted Detect candidates and detect-policy eval/optimize runs.

Each report run also writes a persistent artifact bundle under `report_runs/<timestamp>-<slug>/` so the lead plan and subagent outputs are stored outside the live prompt chain, following Anthropic's external-memory / artifact pattern.

## Architecture wireframe

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                                Data Sources                                 │
│  RSS feeds from feeds/rss.md                    YouTube channels + defuddle.md│
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              Ingestion Layer                                │
│  main.py::run_ingest                                                        │
│   • fetch_rss() + fetch_youtube()                                           │
│   • optional full-text extraction (article_extractor.py)                    │
│   • chunk_and_embed() → source_chunks (pgvector embeddings)                 │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              Storage Layer                                  │
│  Postgres tables: sources, source_chunks, trend_candidates, reports,        │
│  trend_feedback, trend_candidate_sources                                    │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              Detection Layer                                │
│  main.py::run_detect                                                        │
│   1) BERTrend weak-signal detector (trend_detection.py)                     │
│   2) tactical pattern novelty detector                                      │
│      (tactical_extraction.py + novelty_scoring.py)                          │
│   3) LLM-only fallback detector                                              │
│  scoring = base score + feedback adjustment + novelty adjustment            │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                           Report Generation Layer                           │
│  main.py::run_report / generate_report                                      │
│   • quality gate (min score + source diversity)                             │
│   • planner (LeadResearcher) + persisted lead plan                          │
│   • parallel OODA subagents + per-agent artifact bundles                    │
│   • synthesis + sufficiency evaluation                                      │
│   • citation verification + final revision                                  │
│   • report_runs/ artifacts for replay/debugging                             │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                               Outputs & Ops                                 │
│  • reports table + local markdown reports/                                  │
│  • dashboard + APIs (server.py: /api/dashboard, /api/run-step,             │
│    /api/trend-feedback)                                                     │
│  • cron or manual CLI step execution                                        │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Requirements

- Python 3.11+
- Postgres with `pgvector`
- Cloudflare AI Gateway URL + token (OpenAI-compatible API endpoint)
- internet access to `defuddle.md` for RSS article extraction and YouTube transcript fetches

Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

Copy env template:

```bash
cp env.example .env
```

Required environment variables (see `env.example`):

- `CLOUDFLARE_GATEWAY_URL`
- `CLOUDFLARE_GATEWAY_TOKEN`
- database connection (`DATABASE_URL` or Railway-style `PG*` variables)

Optional ingest safety knobs:

- `RSS_OVERLAP_SECONDS` (default `172800`) adds a 48-hour overlap to incremental RSS fetches.
- `YOUTUBE_OVERLAP_SECONDS` (default `172800`) adds a 48-hour overlap to per-channel YouTube publication watermarks.
- `RSS_FETCH_MAX_WORKERS` (default `2`) limits concurrent RSS feed fetches.
- `RSS_FEED_MIN_INTERVAL_SECONDS` (default `0.75`) spaces out RSS feed requests.
- `DEFUDDLE_MIN_INTERVAL_SECONDS` (default `2.0`) spaces out Defuddle article/transcript requests.
- `EMBED_MIN_INTERVAL_SECONDS` (default `1.0`) spaces out embedding API calls.

Model selection defaults come from `config.json` and can be overridden via env vars (`MODEL`, `LEAD_MODEL`, `EMBED_MODEL`, etc.). Use exact provider-prefixed model IDs in `config.json` and env vars; the app no longer rewrites alias model names at runtime.

Cloudflare AI Gateway note:

- this repo is configured to work with AI Gateway Unified Billing for Anthropic and Workers AI
- the default lead/report model is `anthropic/claude-sonnet-4-6` because it works on the unified `/compat` route in this setup
- DeepSeek is not a default path here; if you intentionally switch to a DeepSeek model, treat it as a BYOK/provider-specific configuration unless your Cloudflare account explicitly supports it on Unified Billing

## Database setup

1. Enable `vector` extension in Postgres/Supabase.
2. Apply schema:

```bash
psql "$DATABASE_URL" -f sql/schema.sql
```

3. If upgrading an existing database, also run:

```bash
psql "$DATABASE_URL" -f sql/migrate_multilang.sql
```

## Feed configuration

- RSS feed list used by ingestion: `feeds/rss.md`
- YouTube source list actually used by ingestion: `feeds/youtube.md`

`feeds/youtube.md` format:

```text
Channel Name: https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx
```

The ingest step normalizes YouTube sources to canonical `/channel/UC...` URLs.
It now resolves non-canonical YouTube source URLs at ingest time without rewriting `feeds/youtube.md`.

## CLI usage

Run one step:

```bash
python main.py --step ingest
python main.py --step backfill
python main.py --step detect
python main.py --step rescore
python main.py --step report
```

Run combined:

```bash
python main.py --step all
```

Notes:

- default step is `ingest`.
- `--step all` runs ingest + detect.
- `--step backfill` reprocesses recent sources that are missing chunk embeddings.
- `--step rescore` recomputes `novelty_score` and `final_score` for historical `trend_candidates` using the current novelty criteria.
- add `--allow-report-after-detect` to include report generation in the same run.
- `--backfill-days N` and `--backfill-limit N` control the backfill scan/reprocess window.
- `--min-new-sources-for-detect N` skips detect when latest ingest added fewer than `N` sources.
- `--rescore-limit N`, `--rescore-batch-size N`, and `--rescore-statuses a,b,c` control historical trend rescoring.

## Dashboard server

Start local dashboard:

```bash
python server.py
```

Default URL: `http://localhost:8080/`

The dashboard can:

- show ingest/detect/report records,
- show run status and metrics,
- trigger pipeline steps (`/api/run-step`),
- rescore historical trend candidates with the current novelty logic,
- record trend feedback (`/api/trend-feedback`).

## Detect-policy tuning loop

The detect layer now has a local, non-LLM tuning loop. It does not call paid APIs.

Live policy files:

- `detect_policy.py`
- `detect_policy_config.json`

Harness files:

- `autoresearch/pipeline.py`
- `autoresearch/ingest/optimize_ingest_policy.py`
- `autoresearch/detect/program.md`
- `autoresearch/detect/eval_detect.py`
- `autoresearch/detect/evaluator.py`
- `autoresearch/detect/export_candidates_snapshot.py`
- `autoresearch/detect/optimize_detect_policy.py`
- `autoresearch/detect/fixtures/candidates.json`
- `autoresearch/report/program.md`
- `autoresearch/report/eval_report.py`
- `autoresearch/report/evaluator.py`
- `autoresearch/report/export_reports_snapshot.py`
- `autoresearch/report/benchmark_report.py`
- `autoresearch/report/optimize_report_policy.py`

Run the evaluator on the starter fixture:

```bash
.venv/bin/python autoresearch/detect/eval_detect.py
```

Run the report-quality evaluator against recent reports from Postgres:

```bash
.venv/bin/python autoresearch/report/eval_report.py --refresh-auto
```

Tune ingest policy from observed source lag and daily source volume:

```bash
.venv/bin/python autoresearch/ingest/optimize_ingest_policy.py --apply
```

Run the full no-LLM hourly autoresearch loop:

```bash
.venv/bin/python autoresearch/pipeline.py
```

Benchmark a small fixed set of recent report topics under candidate report
policies and compare generated output scores. This is a manual LLM-backed check,
not part of the hourly no-LLM autoresearch loop:

```bash
.venv/bin/python autoresearch/report/benchmark_report.py --refresh-auto --limit 3
```

Search for the best report policy on recent report metrics and apply it to the
live pipeline:

```bash
.venv/bin/python autoresearch/report/optimize_report_policy.py --refresh-auto --limit 2
```

The report optimizer now has a built-in safety rule: it only applies a new
policy when the best candidate beats the baseline by at least the configured
minimum improvement. The daily optimize path is no-LLM: it scores recent stored
reports, simulates candidate policies mathematically, and writes the winning
policy back for future main-pipeline runs. Each run is also persisted to Postgres in
minimum improvement. The daily optimize path is no-LLM: it scores recent stored
reports, simulates candidate policies mathematically, and writes the winning
policy back for future main-pipeline runs. Each run is also persisted to Postgres in
`report_policy_runs`, and summary fields are mirrored into `pipeline_state` for
dashboard/debug visibility.

It is also cost-aware: report policy config now includes a per-report LLM budget
target (`max_report_llm_cost_usd`), and the optimizer prefers the best-scoring
candidate that fits inside that budget. If no candidate fits, it falls back to
the best quality-per-dollar option and records that decision explicitly.

Export a real candidate snapshot for manual labeling:

```bash
.venv/bin/python autoresearch/detect/export_candidates_snapshot.py \
  --output autoresearch/detect/fixtures/live_candidates.json
```

Export a snapshot with obvious labels inferred from report status and feedback:

```bash
.venv/bin/python autoresearch/detect/export_candidates_snapshot.py \
  --label-mode auto \
  --output autoresearch/detect/fixtures/live_candidates.auto.json
```

Evaluate any labeled fixture:

```bash
.venv/bin/python autoresearch/detect/eval_detect.py \
  --fixture autoresearch/detect/fixtures/live_candidates.json
```

Search for better detect settings and apply them to the live pipeline:

```bash
.venv/bin/python autoresearch/detect/optimize_detect_policy.py \
  --refresh-auto \
  --apply
```

That tuning command exports an auto-labeled snapshot from the DB, searches a grid of scoring and gate settings, and writes the best result back to `detect_policy_config.json`. Because the main detect flow and dashboard feedback path both read that config, the tuned policy affects live candidate ranking and report gating for future runs.

## Suggested cron split

```bash
0 * * * * cd /path/to/research && /path/to/python main.py --step ingest >> logs/ingest.log 2>&1
15 * * * * cd /path/to/research && /path/to/python autoresearch/pipeline.py >> logs/autoresearch.log 2>&1
0 */6 * * * cd /path/to/research && /path/to/python main.py --step detect --min-new-sources-for-detect 5 >> logs/detect.log 2>&1
0 1 * * * cd /path/to/research && /path/to/python main.py --step report >> logs/report.log 2>&1
```

## Key files

- `main.py` – end-to-end pipeline logic
- `server.py` – dashboard + step runner API
- `trend_detection.py` – BERTrend-inspired signal detection
- `tactical_extraction.py` – football-aware tactical parsing/chunk context
- `novelty_scoring.py` – novelty baseline + scoring
- `article_extractor.py` – RSS/full-text extraction logic
- `db_conn.py` – resilient DB conninfo resolution
- `sql/schema.sql` – schema and search functions
