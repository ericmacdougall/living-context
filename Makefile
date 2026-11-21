# Developer shortcuts. Nothing here calls a model or a paid API, and nothing needs Elasticsearch.
#
#   make venv                      python3.13 venv with the light dependencies (numpy, cryptography, pytest, pytest-cov); no torch
#   make test                      the unit suite (tests that need Elasticsearch, the embedder or a model skip with a reason)
#   make cov                       the unit suite with a coverage report for living_context and evalkit (term + coverage.json)
#   make example                   examples/quickstart.py, offline
#   make bench-metrics RUN=<dir>   recall/precision/all-gold/MRR per arm and cost/latency per task from a run directory
#
# VENV defaults to ./.venv (ignored by git); to reuse another one: make test VENV=~/lc-phase1/venv
PYTHON ?= python3.13
VENV   ?= .venv
PY     := $(VENV)/bin/python
PYTEST := $(PY) -m pytest -p no:cacheprovider
TIMEOUT ?= 1800

.PHONY: venv test cov example bench-metrics

venv:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --quiet numpy cryptography -r requirements-test.txt

test:
	$(PYTEST) tests/unit -q --timeout=$(TIMEOUT)

cov:
	$(PYTEST) tests/unit -q --timeout=$(TIMEOUT) --cov=living_context --cov=evalkit --cov-report=term --cov-report=json:coverage.json

example:
	$(PY) examples/quickstart.py

bench-metrics:
	@test -n "$(RUN)" || { echo "usage: make bench-metrics RUN=<run-dir>"; exit 2; }
	$(PY) -m bench.metrics "$(RUN)"
