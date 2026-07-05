# Makefile — one command reproduces every gate (producibility, repeatability).
# `make verify` is the full merge gate; CI runs exactly these targets, so green
# locally means green in CI (CICD-27).

.DEFAULT_GOAL := help
.PHONY: help install lint format type test cov security serve verify clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Sync the environment (uv, frozen lock — CQ-09)
	uv sync --all-extras --frozen

lint: ## Static analysis (ruff): correctness, security, import hygiene
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
	uv run pytest --cov --cov-report=term-missing --cov-fail-under=85

security: ## Dependency + secret scan (SEC-11/17)
	uv run pip-audit
	gitleaks detect --source . --config .gitleaks.toml --no-banner --redact

serve: ## Run the dev server
	uv run encore serve

# The full gate. Determinism + reproducibility: same inputs, same result, every run.
verify: lint type cov security ## Run the complete merge gate (format+lint + type + test/cov + security)
	@echo "verify: all gates green"

clean: ## Remove caches and build artifacts
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache *.egg-info src/*.egg-info htmlcov coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
