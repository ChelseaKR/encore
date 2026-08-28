# Makefile — one command reproduces every gate (producibility, repeatability).
# `make verify` is the full merge gate; CI runs exactly these targets, so green
# locally means green in CI (CICD-27).

.DEFAULT_GOAL := help
.PHONY: help install lint format type test cov security responsible todo-gate slo-check citation-check i18n-check i18n-extract wheel serve verify clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Sync the environment (uv, locked lock, dev group — CQ-09/CQ-27)
	# `--locked`, not `--frozen`. `--frozen` installs straight from uv.lock without
	# reading pyproject.toml, so it cannot compare the two and exits 0 on a lock that
	# no longer satisfies the manifest — the CQ-09 lockfile control could not fail.
	# `--locked` resolves against pyproject.toml and exits 1 when they disagree.
	uv sync --locked --all-extras --group dev

lint: ## Static analysis (ruff + shellcheck): correctness, security, import hygiene, complexity
	# `scripts` is in scope deliberately. It holds gate logic — todo-gate,
	# i18n-check, the SLO validator, the semgrep-test and external-refs gates —
	# and gate code held to a lower standard than the code it gates is how a
	# gate quietly stops working. Extending ruff here immediately caught a
	# complexity violation in one of them.
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts
	# Same reasoning for the shell gates: a swallowed exit code or an unquoted
	# expansion in todo-gate.sh would disarm a merge gate silently.
	shellcheck scripts/*.sh

format: ## Auto-format
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

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
	# History mode (above) cannot see a secret that is written but not yet
	# committed: measured exit 0 with a live-shaped AWS key sitting in the
	# working tree. CI is covered because it scans an already-committed tree
	# with fetch-depth: 0 — but the local `make verify` a contributor runs
	# BEFORE committing was not, and the pre-commit hook only helps if they
	# ran `uvx pre-commit install`. Scan the working tree too (~0.4s).
	gitleaks detect --source . --config .gitleaks.toml --no-banner --redact --no-git
	uvx --from semgrep==1.166.0 semgrep scan \
		--config .semgrep-rules --config p/default --config p/python \
		--severity ERROR --error --metrics off --disable-nosem src tests
	@./scripts/semgrep-test-gate.sh

responsible: ## Stage-8 responsible-tech guards: read-only Plex, no-secrets-in-logs, no-outing (M1, DoD §privacy)
	uv run pytest -q -m "read_only_plex or no_secrets_in_logs or no_outing"

slo-check: ## Validate slos/*.yaml against the Observability Standard §4 schema (OBS-14)
	uv run python scripts/validate_slos.py slos/

citation-check: ## Validate CITATION.cff (DOC-08) — pinned cffconvert via uvx
	uvx cffconvert==2.0.0 --validate -i CITATION.cff

i18n-check: ## I18N G2-lite (docs/I18N.md): the committed messages.pot is current
	@./scripts/i18n-check.sh

i18n-extract: ## Regenerate src/encore/locales/encore.pot from the source strings
	uv run pybabel extract --mapping-file babel.cfg --keyword _ --keyword _n:1,2 \
		--omit-header --sort-output --no-location \
		--output-file src/encore/locales/encore.pot src

wheel: ## Build sdist + wheel (CQ-10) — proves the package builds, container isn't the only artifact
	uv build

todo-gate: ## Fail on TODO/FIXME/HACK with no author + issue-or-milestone ref (CQ-34/35)
	@./scripts/todo-gate.sh

serve: ## Run the dev server
	uv run encore serve

# The full gate. Determinism + reproducibility: same inputs, same result, every run.
# CI runs this exact target (ci.yml/release.yml) — there is no second, drifted
# implementation of "the merge gate" anywhere (CICD-27).
verify: lint type cov security responsible todo-gate slo-check citation-check i18n-check wheel ## Run the complete merge gate (format+lint + type + test/cov + security + stage-8 guards + todo-gate + slo/citation/i18n schema checks + wheel build)
	@echo "verify: all gates green"

clean: ## Remove caches and build artifacts
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache *.egg-info src/*.egg-info htmlcov coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
