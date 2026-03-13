PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python; fi)

.PHONY: help test-detect eval-detect eval-report benchmark-report optimize-report-policy dashboard step-ingest step-backfill step-detect step-rescore step-report

help:
	@printf "Targets:\n"
	@printf "  make test-detect   Run detect-related unit tests\n"
	@printf "  make eval-detect   Run offline detect-policy evaluation\n"
	@printf "  make eval-report   Run offline report-quality evaluation\n"
	@printf "  make benchmark-report Run report-policy benchmark on recent reports\n"
	@printf "  make optimize-report-policy Search/apply the best report policy on recent reports\n"
	@printf "  make dashboard     Start the local dashboard server\n"
	@printf "  make step-ingest   Run ingest\n"
	@printf "  make step-backfill Run backfill\n"
	@printf "  make step-detect   Run detect\n"
	@printf "  make step-rescore  Run rescore\n"
	@printf "  make step-report   Run report\n"

test-detect:
	$(PYTHON) -m unittest \
		tests.test_pipeline_helpers \
		tests.test_novelty_scoring \
		tests.test_detect_policy \
		tests.test_detect_evaluator

eval-detect:
	$(PYTHON) autoresearch_detect/eval_detect.py

eval-report:
	$(PYTHON) autoresearch_report/eval_report.py --refresh-auto

benchmark-report:
	$(PYTHON) autoresearch_report/benchmark_report.py --refresh-auto --limit 3

optimize-report-policy:
	$(PYTHON) autoresearch_report/optimize_report_policy.py --refresh-auto --limit 3

dashboard:
	$(PYTHON) server.py

step-ingest:
	$(PYTHON) main.py --step ingest

step-backfill:
	$(PYTHON) main.py --step backfill

step-detect:
	$(PYTHON) main.py --step detect

step-rescore:
	$(PYTHON) main.py --step rescore

step-report:
	$(PYTHON) main.py --step report
