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

Some feeds block specific bot user-agents and return `HTTP 403`. Configure `RSS_FEED_USER_AGENTS` to rotate through one or more values (separate with `||`) during ingestion retries.

```bash
RSS_FEED_USER_AGENTS="Mozilla/5.0 (...)||Feedly/1.0 (+http://www.feedly.com/fetcher.html)"
```

## YouTube TranscriptAPI ingestion logging

Set `RESEARCH_LOG_LEVEL` to control runtime log verbosity across pipeline stages. The ingestion workflow sets this to `INFO` and now logs TranscriptAPI channel/video request attempts, response summaries, retries, and per-channel completion metrics to make silent YouTube failures diagnosable from `logs/ingestion.log`.

```bash
RESEARCH_LOG_LEVEL=INFO
```

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
