# Setup Guide

No terminal required. Everything is done through web UIs.

---

## What you need

- A GitHub account with access to this repository
- A Supabase project (free tier works)
- API keys for: Anthropic, OpenAI, and your transcript provider

---

## 1) Set up the database (Supabase SQL Editor)

Open your Supabase project → **SQL Editor**, then run each file in order:

1. Copy and paste the contents of `sql/001_init.sql` → **Run**
2. Copy and paste the contents of `sql/002_vector_indexes.sql` → **Run**
3. Copy and paste the contents of `sql/003_hybrid_search.sql` → **Run**

Then copy your **Postgres connection string** from Supabase: **Project Settings → Database → Connection string → URI tab**. It looks like `postgresql://postgres:[YOUR-PASSWORD]@db.<project-ref>.supabase.co:5432/postgres`.

---

## 2) Add GitHub Actions secrets

Go to your repository on GitHub → **Settings → Secrets and variables → Actions → New repository secret**.

Add each of the following:

**Required:**

| Secret | Value |
|---|---|
| `SUPABASE_URL` | `https://<project>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | From Supabase → Project Settings → API → Project API keys → **`service_role` (Secret)** key |
| `POSTGRES_DSN` | From Supabase → Project Settings → Database → Connection string → URI tab (see step 1) |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `OPENAI_API_KEY` | Your OpenAI API key |
| `TRANSCRIPT_API_KEY` | Your transcript provider key |
| `DELIVERY_GITHUB_TOKEN` | A GitHub personal access token with repo write access |
| `RSS_FEEDS` | e.g. `OpenAI Blog\|https://openai.com/blog/rss.xml,Anthropic News\|https://www.anthropic.com/news/rss.xml` |
| `YOUTUBE_CHANNELS` | e.g. `OpenAI\|UCXZCJLdBC09xxGZ6gcdrc6A` |
| `REPORT_TOPIC` | e.g. `Weekly AI research roundup` |

**Optional delivery:**

| Secret | Value |
|---|---|
| `DELIVERY_EMAIL_ENABLED` | `true` or `false` |
| `DELIVERY_EMAIL_API_KEY` | Email provider API key |
| `DELIVERY_EMAIL_FROM` | Sender address |
| `DELIVERY_EMAIL_TO` | Recipient address |
| `DELIVERY_SLACK_ENABLED` | `true` or `false` |
| `DELIVERY_SLACK_WEBHOOK_URL` | Slack incoming webhook URL |

---

## 3) Run the pipeline

Go to your repository → **Actions → Weekly Report Pipeline → Run workflow**.

Choose a starting stage (default: `ingestion` runs everything) and tap **Run workflow**.

The pipeline runs automatically every Monday at noon UTC. You can also trigger it manually any time from the Actions tab.

---

## Troubleshooting

- **Job fails on a specific stage**: Click the failed job in Actions to see the logs. Each stage uploads a log file as an artifact.
- **`Missing required environment variable`**: A secret is missing or misspelled in step 2.
- **Database errors**: Verify the SQL files ran without errors in the Supabase SQL Editor and that `POSTGRES_DSN` is correct.
- **No ingestion records**: Check the formatting of `RSS_FEEDS` and `YOUTUBE_CHANNELS` — use `Name|url` pairs separated by commas.
- **Delivery not publishing**: Confirm `DELIVERY_GITHUB_TOKEN` has write access to the repo.
