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

## Next step

Use the benchmark runner to regenerate a small fixed set of reports under a
small number of deliberate candidate `report_policy_config.json` settings,
then compare outcomes with this evaluator. Keep the candidate set small enough
to be reasonable on Railway.
