PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python; fi)

.PHONY: help test-detect eval-detect dashboard step-ingest step-backfill step-detect step-rescore step-report

help:
	@printf "Targets:\n"
	@printf "  make test-detect   Run detect-related unit tests\n"
	@printf "  make eval-detect   Run offline detect-policy evaluation\n"
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
