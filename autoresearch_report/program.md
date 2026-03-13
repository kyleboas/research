# Report Quality Evaluation

This directory is the Railway-safe foundation for an `autoresearch` loop for the
report stage.

## Goal

Evaluate recently generated reports using a deterministic, repeatable harness
that runs entirely from the repo plus Postgres:

- export recent reports from the `reports` table
- validate the citation IDs they reference
- score them for structural completeness, citation health, source diversity,
  and overall thoroughness

## Why this differs from detect

The detect harness can directly tune a scoring policy against a frozen fixture,
because the policy itself determines the ranking outcome.

The report stage is different: changing report policy changes generation, not
just scoring. That means a true optimization loop must:

1. run report generation under a candidate policy
2. evaluate the resulting report
3. compare against prior runs

This directory implements step 2 and the DB-backed export needed for step 1.

## Current loop

1. Export recent reports from Postgres:

```bash
../.venv/bin/python export_reports_snapshot.py --output fixtures/recent_reports.json
```

2. Evaluate them:

```bash
../.venv/bin/python eval_report.py --fixture fixtures/recent_reports.json
```

Or do both in one command:

```bash
../.venv/bin/python eval_report.py --refresh-auto
```

## Constraints

- Do not depend on local `report_runs/` surviving between Railway runs
- Treat the exported fixture as the frozen input for evaluation
- Keep scoring interpretable and deterministic

## Live loop

The repo now has a basic closed loop for report policy tuning:

1. `benchmark_report.py --refresh-auto --limit 3`
   regenerates a small fixed set of reports under candidate policies and
   compares the resulting scores.
2. `optimize_report_policy.py --refresh-auto --limit 3`
   runs the same search but applies the best policy back to
   `report_policy_config.json` only when the best result clears the minimum
   improvement threshold.
3. Railway/dashboard can trigger report policy eval, benchmark, and optimize
   runs, and Discord receives summaries when those runs finish.

Every benchmark/optimize run is also stored in Postgres (`report_policy_runs`)
so the tuning loop has a persistent history instead of relying only on logs or
TSV artifacts.

The optimizer is now budget-aware as well:

- `report_policy_config.json` includes `max_report_llm_cost_usd`
- candidate policies get an estimated per-report LLM cost
- the loop prefers the highest-quality candidate that stays within budget
- if none fit, it falls back to the best quality-per-dollar candidate

Keep the benchmark topic count small enough to remain reasonable on Railway.
