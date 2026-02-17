# Operations Runbook

This runbook standardizes how to execute, replay, and recover the research pipeline.

## Prerequisites
- Python environment with project dependencies installed.
- PostgreSQL available and initialized with `sql/001_init.sql`.
- Environment variables set for ingestion, LLMs, and delivery integrations.

## Local run (manual)
Run stages independently while preserving one `pipeline_run_id`.

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

Notes:
- Use `--dry-run` in delivery for validation-only publishing.
- Artifacts are written under `artifacts/reports/<pipeline_run_id>/`.

## Scheduled run
Use your scheduler/CI to trigger stages in order with shared run ID.

Recommended schedule profiles:
- **Weekly:** every 7 days; lowest freshness/cost.
- **Multi-weekly (e.g., 3x/week):** higher freshness and proportionally higher token spend.

Operational requirements for scheduled runs:
1. Generate and export one run ID per scheduled execution.
2. Fail fast on stage error.
3. Persist logs/artifacts from each stage for triage.
4. Keep delivery in `dry-run` for non-production branches.

## Replay procedure
Replay is used when:
- A stage failed and upstream data should be reused.
- You need reproducibility with the same run ID.

Replay options:
- **Stage replay:** rerun only failed stage with same `--pipeline-run-id`.
- **Downstream replay:** rerun failed stage plus all downstream stages.

Examples:
```bash
python -m src.pipeline embedding --pipeline-run-id "$RUN_ID"
python -m src.pipeline generation --pipeline-run-id "$RUN_ID"
python -m src.pipeline verification --pipeline-run-id "$RUN_ID"
```

## Failure recovery

### 1) Ingestion failures
- Symptoms: feed/channel fetch failures or missing transcript list in logs.
- Recovery:
  1. Fix network/API credential issues.
  2. Rerun ingestion with same run ID.
  3. Continue downstream stages.

### 2) Embedding failures
- Symptoms: embedding API errors, partial vector writes.
- Recovery:
  1. Fix model/API configuration.
  2. Rerun embedding; query is idempotent and only stale/missing rows are embedded.

### 3) Generation failures
- Symptoms: prompt/model runtime errors, missing report artifacts.
- Recovery:
  1. Verify Anthropic/OpenAI credentials and model IDs.
  2. Rerun generation with same run ID.
  3. Re-run verification after generation success.

### 4) Verification failures
- Symptoms: claims table/report verification metadata not updated.
- Recovery:
  1. Ensure final report exists for run ID.
  2. Rerun verification.

### 5) Delivery failures
- Symptoms: publish/email/slack error while report exists.
- Recovery:
  1. Validate integration credentials.
  2. Retry delivery with same run ID and optional `--dry-run`.

## Cost telemetry checks
Each stage persists cost telemetry under `pipeline_runs.cost_estimate_json` (or `metadata.cost_estimate_json` fallback if schema lacks the dedicated column):
- `stages.<stage>.token_count`
- `stages.<stage>.estimated_cost_usd`
- rollups: `total_token_count`, `total_estimated_cost_usd`

Example inspection query:
```sql
SELECT run_name, status, cost_estimate_json
FROM pipeline_runs
ORDER BY id DESC
LIMIT 20;
```
