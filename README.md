# Football Research Pipeline

Automatic deep research system that ingests football content, detects novel tactical trends, and generates production-grade sourced reports using multi-agent orchestration.

Architecture mirrors [Anthropic's multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system), implemented here with OpenAI models authenticated via ChatGPT OAuth.

## How it works

1. **Ingest** — Fetches RSS feeds and YouTube transcripts every hour (top of hour), stores full content in Supabase Postgres with vector embeddings.
2. **Detect** — Uses OpenAI models (via ChatGPT OAuth) to identify novel tactics being tried by players/teams before they become mainstream.
3. **Report** — Multi-agent deep research pipeline:

```
┌─────────────────────────────────────────────────────┐
│ LeadResearcher (Opus + extended thinking)            │
│ - Assesses complexity (simple/moderate/complex)      │
│ - Decomposes into non-overlapping research angles    │
│ - Calibrates subagent count + retrieval depth        │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│ Parallel Subagents (Sonnet × N)                     │
│ OODA loop per angle:                                │
│   Observe: hybrid search (semantic + keyword RRF)   │
│   Orient:  evaluate coverage vs objective           │
│   Decide:  generate narrower query or stop          │
│   Act:     retrieve again (up to 5 rounds)          │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│ Synthesis                                           │
│ Merge all subagent outputs into cohesive draft      │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│ Sufficiency Evaluation (LeadResearcher re-planning) │
│ - Evaluates draft quality with extended thinking    │
│ - If gaps found: spawn MORE subagents → re-synth   │
│ - Up to 2 research rounds                          │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│ CitationAgent                                       │
│ Verifies every [S:C] tag maps to real evidence      │
│ Flags hallucinated IDs, uncited claims, mismatches  │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│ Revision Editor                                     │
│ Applies all citation fixes, qualifies speculation,  │
│ produces final publication-quality report            │
└─────────────────────────────────────────────────────┘
```

LLM calls use your **ChatGPT subscription OAuth login** (no LLM API keys required).

LLM calls are routed through Cloudflare AI Gateway, and the runtime normalizes gateway URLs so OpenAI SDK path appending does not produce malformed endpoints (for example duplicated `/chat/completions`).

For embeddings, the pipeline always sends OpenAI-compatible payloads (`model` + `input`) and auto-normalizes model names for `/compat` routes (for example `@cf/baai/bge-m3` becomes `workers-ai/@cf/baai/bge-m3`).

The runtime sends `cf-aig-authorization: Bearer <CLOUDFLARE_GATEWAY_TOKEN>` with gateway requests.

## Setup

1. Run `sql/schema.sql` in your Supabase SQL editor (enable pgvector first).
2. Copy `env.example` to `.env` and set database/transcript variables.
3. `pip install -r requirements.txt`
4. Run ingest manually once: `python main.py --step ingest`
5. Detect candidates: `python main.py --step detect`
6. Generate a report from top pending candidate: `python main.py --step report`

## Cron (recommended split schedules)

```bash
0 * * * * cd /path/to/research && /path/to/python main.py --step ingest >> logs/ingest.log 2>&1
0 */6 * * * cd /path/to/research && /path/to/python main.py --step detect --min-new-sources-for-detect 5 >> logs/detect.log 2>&1
0 1 * * * cd /path/to/research && /path/to/python main.py --step report >> logs/report.log 2>&1
```

`python main.py` defaults to `--step ingest`.

`--step all` now runs ingest + detect only. To explicitly allow same-process reporting, pass `--allow-report-after-detect`.

## Feeds

Edit `feeds/rss.md` and `feeds/youtube.md` to add/remove sources.

## Files

```
main.py              # entire pipeline (single file)
sql/schema.sql       # 3 tables + hybrid RRF search function
feeds/rss.md         # RSS feed list
feeds/youtube.md     # YouTube channel list
reports/             # generated reports (gitignored)
```
