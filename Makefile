# Makefile — one command reproduces every gate (producibility, repeatability).
# `make verify` is the full merge gate; CI runs exactly these targets, so green
# locally means green in CI (CICD-27).

.DEFAULT_GOAL := help
.PHONY: help install lint format type test cov security responsible todo-gate slo-check citation-check wheel serve verify clean

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

security: ## SAST + dependency + secret scans (SEC-07/11/13/17)
	uv run pip-audit
	osv-scanner scan source --lockfile uv.lock
	gitleaks detect --source . --config .gitleaks.toml --no-banner --redact
	uvx --from semgrep==1.166.0 semgrep scan \
		--config .semgrep-rules --config p/default --config p/python \
		--severity ERROR --error --metrics off --disable-nosem src tests
	uvx --from semgrep==1.166.0 semgrep test .semgrep-rules

responsible: ## Stage-8 responsible-tech guards: read-only Plex, no-secrets-in-logs, no-outing (M1, DoD §privacy)
	uv run pytest -q -m "read_only_plex or no_secrets_in_logs or no_outing"

slo-check: ## Validate slos/*.yaml against the Observability Standard §4 schema (OBS-14)
	uv run python scripts/validate_slos.py slos/

citation-check: ## Validate CITATION.cff (DOC-08) — pinned cffconvert via uvx
	uvx cffconvert==2.0.0 --validate -i CITATION.cff

wheel: ## Build sdist + wheel (CQ-10) — proves the package builds, container isn't the only artifact
	uv build

todo-gate: ## Fail on TODO/FIXME/HACK with no author + issue-or-milestone ref (CQ-34/35)
	@./scripts/todo-gate.sh

serve: ## Run the dev server
	uv run encore serve

# The full gate. Determinism + reproducibility: same inputs, same result, every run.
# CI runs this exact target (ci.yml/release.yml) — there is no second, drifted
# implementation of "the merge gate" anywhere (CICD-27).
verify: lint type cov security responsible todo-gate slo-check citation-check wheel ## Run the complete merge gate (format+lint + type + test/cov + security + stage-8 guards + todo-gate + slo/citation schema checks + wheel build)
	@echo "verify: all gates green"

clean: ## Remove caches and build artifacts
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache *.egg-info src/*.egg-info htmlcov coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
