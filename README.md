# Research

Autonomous weekly research pipeline for LLM ecosystem updates. The system ingests RSS + YouTube sources, chunks and embeds content for hybrid retrieval, generates a cited markdown report through multi-pass drafting, verifies extracted claims, and publishes output to delivery channels.

## What it does

- **Ingestion**: pulls configured RSS feeds and YouTube channel transcripts.
- **Processing**: performs deterministic chunking and embedding upserts.
- **Generation**: runs research/draft/critique/revision passes to produce a final report.
- **Verification**: extracts claims and scores support confidence.
- **Delivery**: supports GitHub, email, and Slack publishing flows.

## Repository layout

- `src/` — pipeline stages and integrations.
- `sql/` — schema, vector indexes, and hybrid retrieval SQL.
- `feeds/` — source-list markdown files.
- `tests/` — unit and flow tests.
- `docs/` — setup, runbook, architecture, and planning docs.

## Setup

Use the iPhone-friendly setup guide (works for desktop/codespaces too):

- [`docs/setup.md`](docs/setup.md)

## Run the pipeline

After configuring environment variables and database schema:

```bash
RUN_ID="$(python - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"

python -m src.pipeline ingestion --pipeline-run-id "$RUN_ID"
python -m src.pipeline embedding --pipeline-run-id "$RUN_ID"
python -m src.pipeline generation --pipeline-run-id "$RUN_ID"
python -m src.pipeline verification --pipeline-run-id "$RUN_ID"
python -m src.pipeline delivery --pipeline-run-id "$RUN_ID" --dry-run
```

Or run all stages in one command:

```bash
python -m src.pipeline all --pipeline-run-id "$RUN_ID"
```

## RSS troubleshooting

Some feeds (notably Substack/Cloudflare-protected feeds) block thin non-browser requests and return `HTTP 403`. The RSS client now sends a browser-like baseline header set and logs response snippets for `HTTPError` failures to help diagnose WAF blocks.

Set one consistent user-agent with `RSS_FEED_USER_AGENT` (preferred):

```bash
RSS_FEED_USER_AGENT="Mozilla/5.0 (compatible; ResearchBot/1.0; +https://github.com/kyleboas/research)"
```

`RSS_FEED_USER_AGENTS` is still supported for backwards compatibility, but only the first non-empty value is used.

## Testing

```bash
pytest -q
```

## Additional docs

- [`docs/runbook.md`](docs/runbook.md)
- [`docs/research.md`](docs/research.md)
- [`docs/costs.md`](docs/costs.md)
- [`docs/plan.md`](docs/plan.md)
- [`docs/implement.md`](docs/implement.md)
