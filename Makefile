# Makefile — one command reproduces every gate (producibility, repeatability).
# `make verify` is the full merge gate; CI runs exactly these targets, so green
# locally means green in CI (CICD-27).

.DEFAULT_GOAL := help
.PHONY: help install lint format type test cov security todo-gate serve verify clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Sync the environment (uv, frozen lock, dev group — CQ-09/CQ-27)
	uv sync --frozen --all-extras --group dev

lint: ## Static analysis (ruff): correctness, security, import hygiene, complexity
	uv run ruff check src tests
	uv run ruff format --check src tests

format: ## Auto-format
	uv run ruff format src tests
	uv run ruff check --fix src tests

type: ## Strict type checking (mypy)
	uv run mypy

test: ## Run the test suite
	uv run pytest

cov: ## Run tests with a coverage report (branch >=85% — CQ-08)
	uv run pytest --cov --cov-report=term-missing --cov-report=xml --cov-fail-under=85

security: ## Dependency + secret scan (SEC-11/13/17) — audits the *locked* env
	uv run pip-audit
	gitleaks detect --source . --config .gitleaks.toml --no-banner --redact

todo-gate: ## Fail on TODO/FIXME/HACK with no author + issue-or-milestone ref (CQ-34/35)
	@./scripts/todo-gate.sh

serve: ## Run the dev server
	uv run encore serve

# The full gate. Determinism + reproducibility: same inputs, same result, every run.
# CI runs this exact target (ci.yml/release.yml) — there is no second, drifted
# implementation of "the merge gate" anywhere (CICD-27).
verify: lint type cov security todo-gate ## Run the complete merge gate (format+lint + type + test/cov + security + todo-gate)
	@echo "verify: all gates green"

clean: ## Remove caches and build artifacts
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache *.egg-info src/*.egg-info htmlcov coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
