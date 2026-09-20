PY ?= python

.PHONY: help install dev lint fmt test test-all cov eval gate run serve build docker clean

help:
	@echo "install   install the agent into the active environment"
	@echo "dev       install with development extras"
	@echo "lint      ruff check + format check"
	@echo "fmt       ruff format + autofix"
	@echo "test      unit and fixture tests (no live network)"
	@echo "test-all  every test including live-network tests"
	@echo "cov       tests with coverage report"
	@echo "eval      replay the fault-injection corpus and print metrics"
	@echo "gate      replay the corpus and fail if PRD targets regress"
	@echo "run       run the agent in the foreground with the local web UI"
	@echo "build     build sdist and wheel"
	@echo "docker    build the container image"

install:
	$(PY) -m pip install .

dev:
	$(PY) -m pip install -e ".[dev]"

lint:
	$(PY) -m ruff check netpulse tests
	$(PY) -m ruff format --check netpulse tests

fmt:
	$(PY) -m ruff check --fix netpulse tests
	$(PY) -m ruff format netpulse tests

test:
	$(PY) -m pytest -m "not live"

test-all:
	$(PY) -m pytest

cov:
	$(PY) -m pytest -m "not live" --cov=netpulse --cov-report=term-missing

eval:
	$(PY) -m netpulse eval

gate:
	$(PY) -m netpulse eval --gate

run:
	$(PY) -m netpulse run

build:
	$(PY) -m build

docker:
	docker build -t netpulse-local:dev -f packaging/docker/Dockerfile .

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
