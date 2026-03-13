# Football Research Pipeline

Automated football research workflow that:

1. ingests RSS + YouTube transcript content,
2. detects potentially novel tactical trends, and
3. generates citation-checked markdown reports.

The core pipeline lives in `main.py`, with a lightweight dashboard/runner in `server.py`.

## Pipeline overview

### 1) Ingest (`--step ingest`)

- Pulls RSS stories from NewsBlur (`/reader/river_stories`).
- Pulls recent videos from configured YouTube channels via channel RSS feeds.
- Fetches transcripts from TranscriptAPI for discovered videos.
- Runs optional full-text extraction for short RSS bodies (`article_extractor.py`).
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
- final revision.

Final reports are saved to:

- Postgres table `reports`, and
- local `reports/YYYY-MM-DD-<slug>.md`.

## Architecture wireframe

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                                Data Sources                                 │
│  RSS feeds via NewsBlur                      YouTube channels + TranscriptAPI│
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              Ingestion Layer                                │
│  main.py::run_ingest                                                        │
│   • fetch_newsblur() + fetch_youtube()                                      │
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
│   • planner (LeadResearcher)                                                │
│   • parallel OODA subagents                                                 │
│   • synthesis + sufficiency evaluation                                      │
│   • citation verification + final revision                                  │
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
- NewsBlur account credentials
- TranscriptAPI key

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
- `NEWSBLUR_USERNAME`
- `NEWSBLUR_PASSWORD`
- `TRANSCRIPT_API_KEY`
- database connection (`DATABASE_URL` or Railway-style `PG*` variables)

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

- RSS feed list: `feeds/rss.md` (curated reference list)
- YouTube source list actually used by ingestion: `feeds/youtube.md`

`feeds/youtube.md` format:

```text
Channel Name: https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx
```

The ingest step normalizes YouTube sources to canonical `/channel/UC...` URLs.

## CLI usage

Run one step:

```bash
python main.py --step ingest
python main.py --step backfill
python main.py --step detect
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
- add `--allow-report-after-detect` to include report generation in the same run.
- `--backfill-days N` and `--backfill-limit N` control the backfill scan/reprocess window.
- `--min-new-sources-for-detect N` skips detect when latest ingest added fewer than `N` sources.

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
- record trend feedback (`/api/trend-feedback`).

## Suggested cron split

```bash
0 * * * * cd /path/to/research && /path/to/python main.py --step ingest >> logs/ingest.log 2>&1
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
