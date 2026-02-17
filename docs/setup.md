# Setup Guide (iPhone-Only)

This guide is for running the project when your **only device is an iPhone**.

Because this repository is a Python pipeline with Postgres, the practical iPhone approach is to use a **cloud dev environment** (GitHub Codespaces) plus managed services (like Supabase) and do everything from Safari.

---

## What you need

- A GitHub account.
- Access to this repository on GitHub.
- API keys/accounts for:
  - Anthropic
  - OpenAI
  - Transcript provider
  - Supabase (or another Postgres host)

---

## 1) Open a development environment from iPhone

1. Open Safari on iPhone and go to your repository on GitHub.
2. Tap **Code** → **Codespaces** → **Create codespace on main** (or your working branch).
3. Wait for the codespace terminal/editor to load.

> If Codespaces is unavailable on your plan, use any browser-based Linux shell service and run the same commands below.

---

## 2) Install dependencies in the codespace terminal

Run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install anthropic openai "psycopg[binary]" pytest
```

---

## 3) Provision Postgres (Supabase recommended on iPhone)

1. In Safari, open Supabase and create/select your project.
2. Copy the Postgres connection string.
3. In your codespace terminal, set:

```bash
export POSTGRES_DSN="postgresql://<user>:<password>@<host>:<port>/<db>"
```

4. Run DB SQL setup:

```bash
psql "$POSTGRES_DSN" -f sql/001_init.sql
psql "$POSTGRES_DSN" -f sql/002_vector_indexes.sql
psql "$POSTGRES_DSN" -f sql/003_hybrid_search.sql
```

---

## 4) Set required environment variables

In the codespace terminal, export all required variables:

```bash
export SUPABASE_URL="https://<project>.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="<supabase-service-role-key>"
export POSTGRES_DSN="postgresql://<user>:<password>@<host>:<port>/<db>"

export ANTHROPIC_API_KEY="<anthropic-key>"
export ANTHROPIC_MODEL_ID="claude-3-5-sonnet-latest"
export ANTHROPIC_SMALL_MODEL_ID="claude-3-5-haiku-latest"

export OPENAI_API_KEY="<openai-key>"
export OPENAI_EMBEDDING_MODEL="text-embedding-3-large"

export TRANSCRIPT_API_KEY="<transcript-provider-key>"

export GITHUB_TOKEN="<github-token>"
export GITHUB_OWNER="<github-owner-or-username>"
export GITHUB_REPO="<repo-name>"
export GITHUB_DEFAULT_BRANCH="main"
```

Set ingestion sources:

```bash
export RSS_FEEDS="OpenAI Blog|https://openai.com/blog/rss.xml,Anthropic News|https://www.anthropic.com/news/rss.xml"
export YOUTUBE_CHANNELS="OpenAI|UCXZCJLdBC09xxGZ6gcdrc6A"
```

Optional runtime tuning:

```bash
export RSS_FEED_TIMEOUT_S="10"
export RSS_FEED_RETRIES="2"
export RSS_FEED_BACKOFF_BASE_S="0.5"
export YOUTUBE_LATEST_LIMIT="5"
export YOUTUBE_TIMEOUT_S="12"
export CHUNK_WINDOW_SIZE="200"
export CHUNK_OVERLAP="40"
export EMBEDDING_BATCH_SIZE="64"
```

---

## 5) Verify setup

```bash
pytest -q
```

---

## 6) Run the pipeline stage-by-stage

Create one run ID:

```bash
RUN_ID="$(python - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"
```

Run each stage:

```bash
python -m src.pipeline ingestion --pipeline-run-id "$RUN_ID"
python -m src.pipeline embedding --pipeline-run-id "$RUN_ID"
python -m src.pipeline generation --pipeline-run-id "$RUN_ID"
python -m src.pipeline verification --pipeline-run-id "$RUN_ID"
python -m src.pipeline delivery --pipeline-run-id "$RUN_ID" --dry-run
```

Artifacts are written to:

```text
artifacts/reports/<pipeline_run_id>/
```

---

## 7) Optional: one-command run

```bash
python -m src.pipeline all --pipeline-run-id "$RUN_ID"
```

Use `--dry-run` on delivery paths while validating credentials and permissions.

---

## 8) iPhone-specific tips

- In Safari, long-press in terminal to paste API keys.
- Keep a secure password manager open for key copy/paste.
- If terminal sessions reset, re-run `source .venv/bin/activate` and re-export env vars.
- For persistent env vars in Codespaces, use GitHub Codespaces Secrets instead of manual exports.
- Use `--dry-run` first before enabling real delivery actions.

---

## 9) Troubleshooting

- **`Missing required environment variable`**: one or more required `export` commands were missed.
- **`psql: command not found`**: install PostgreSQL client in your cloud dev environment.
- **Database extension/index errors**: verify DB user permissions to create required extensions.
- **No ingestion records**: check `RSS_FEEDS` and `YOUTUBE_CHANNELS` formatting.
- **Delivery errors**: validate GitHub token/repo/branch env vars and retry with `--dry-run` first.
