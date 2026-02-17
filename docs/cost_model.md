# Cost Model

This document defines deterministic cost formulas for per-run and monthly estimates.

## Variables
- `T_embed`: embedding stage tokens.
- `T_gen_in`: generation stage input tokens.
- `T_gen_out`: generation stage output tokens.
- `R_month`: runs per month.
- `M`: model multiplier (`1.0` Sonnet baseline, `5.0` Opus default).

Default rates used by the pipeline:
- Embedding: `C_embed = $0.02 / 1M tokens`
- Sonnet input: `C_sonnet_in = $3.00 / 1M tokens`
- Sonnet output: `C_sonnet_out = $15.00 / 1M tokens`
- Opus multiplier: `M_opus = 5.0`

## Per-stage formulas

### Ingestion
No model billing:

`Cost_ingestion = 0`

### Embedding

`Cost_embedding = (T_embed / 1,000,000) * C_embed`

### Generation (Sonnet baseline)

`Cost_generation_sonnet = (T_gen_in / 1,000,000) * C_sonnet_in + (T_gen_out / 1,000,000) * C_sonnet_out`

### Generation (Opus)

`Cost_generation_opus = Cost_generation_sonnet * M_opus`

### Verification + Delivery
Current implementation records token telemetry but assumes no external billed model calls:

`Cost_verification = 0`

`Cost_delivery = 0`

## Per-run formula

`Cost_run = Cost_ingestion + Cost_embedding + Cost_generation_{tier} + Cost_verification + Cost_delivery`

## Monthly formula

`Cost_month = R_month * Cost_run`

## Weekly vs multi-weekly examples
Assume one run uses:
- `T_embed = 2,000,000`
- `T_gen_in = 1,200,000`
- `T_gen_out = 220,000`

Then:
- `Cost_embedding = (2,000,000 / 1,000,000) * 0.02 = $0.04`
- `Cost_generation_sonnet = (1,200,000 / 1,000,000) * 3 + (220,000 / 1,000,000) * 15 = $6.90`
- `Cost_run_sonnet = $6.94`
- `Cost_run_opus = $6.90 * 5 + $0.04 = $34.54`

Run frequency scenarios:
- **Weekly (4 runs/month)**
  - Sonnet: `4 * 6.94 = $27.76/month`
  - Opus: `4 * 34.54 = $138.16/month`
- **Multi-weekly (12 runs/month, ~3x/week)**
  - Sonnet: `12 * 6.94 = $83.28/month`
  - Opus: `12 * 34.54 = $414.48/month`

## Pipeline telemetry mapping
`pipeline_runs.cost_estimate_json` stores:
- `stages.ingestion|embedding|generation|verification|delivery`
- each stage has `token_count` and `estimated_cost_usd`
- top-level rollups:
  - `total_token_count`
  - `total_estimated_cost_usd`
