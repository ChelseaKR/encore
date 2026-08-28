# Makefile — one command reproduces every gate (producibility, repeatability).
# `make verify` is the full merge gate; CI runs exactly these targets, so green
# locally means green in CI (CICD-27).

.DEFAULT_GOAL := help
.PHONY: help install lint format type test cov security responsible todo-gate slo-check citation-check i18n-check i18n-extract wheel serve verify clean \
        container-tools container-build container-scan container-bringup container-verify \
        external-refs

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

external-refs: ## Ratchet on references to paths outside this repo (issue #22)
	uv run python scripts/external_refs_gate.py

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

# ---------------------------------------------------------------------------
# Stage 9 (container): build + CVE scan + bring-up.
#
# These exist because `make verify` previously could not reproduce them, while
# five documents claimed it reproduced the whole CI gate. ci.yml's `build` job
# ran `docker build`, a Trivy scan with `exit-code: 1`, and a `/livez` probe;
# no Makefile target did any of the three. The Trivy step is the one that has
# actually failed in CI (SEC-28, eight of the last twenty runs), so the single
# gate with a real failure history was the one a contributor could not run.
# Keep these flags identical to ci.yml's trivy-action inputs: divergence here
# recreates exactly the drift this closes.
# ---------------------------------------------------------------------------

# Overridable so a caller can scan a specific build without clobbering a tag.
IMAGE ?= encore:verify
BRINGUP_NAME ?= encore-verify-run
BRINGUP_PORT ?= 8321

container-tools: ## Fail closed when the Stage 9 toolchain is absent (never skip silently)
	@command -v docker >/dev/null 2>&1 || { \
	  echo "container gate: 'docker' not found. Stage 9 (build + Trivy CVE scan + /livez" >&2; \
	  echo "  bring-up) is part of the merge gate because CI runs it and it is the gate" >&2; \
	  echo "  that has actually failed. Install Docker; do not skip this." >&2; exit 1; }
	@docker info >/dev/null 2>&1 || { \
	  echo "container gate: docker is installed but the daemon is not responding." >&2; exit 1; }
	@command -v trivy >/dev/null 2>&1 || { \
	  echo "container gate: 'trivy' not found. Install it (brew install trivy) — the CVE" >&2; \
	  echo "  scan is merge-blocking in ci.yml and must be reproducible locally." >&2; exit 1; }

container-build: container-tools ## Build the OCI image (proves the Dockerfile builds)
	docker build -t $(IMAGE) .

container-scan: container-build ## Trivy CVE scan — CRITICAL/HIGH, fixed-only (SEC-28)
	trivy image --severity CRITICAL,HIGH --ignore-unfixed --exit-code 1 \
		--scanners vuln --skip-version-check $(IMAGE)

container-bringup: container-build ## Start the image and probe /livez (QM-08, OBS-19)
	@docker rm -f $(BRINGUP_NAME) >/dev/null 2>&1 || true
	@docker run -d --name $(BRINGUP_NAME) -p $(BRINGUP_PORT):8321 $(IMAGE) >/dev/null
	@rc=1; for _ in $$(seq 1 40); do \
	  if curl -sf http://127.0.0.1:$(BRINGUP_PORT)/livez >/dev/null 2>&1; then rc=0; break; fi; \
	  sleep 0.5; \
	done; \
	if [ $$rc -ne 0 ]; then \
	  echo "container-bringup: FAIL — $(IMAGE) never answered /livez" >&2; \
	  docker logs $(BRINGUP_NAME) >&2 2>&1 || true; \
	else \
	  echo "container-bringup: OK — /livez answered"; \
	fi; \
	docker rm -f $(BRINGUP_NAME) >/dev/null 2>&1 || true; \
	exit $$rc

container-verify: container-scan container-bringup ## Stage 9 end to end

serve: ## Run the dev server
	uv run encore serve

# The full gate. Determinism + reproducibility: same inputs, same result, every run.
# CI runs this exact target (ci.yml/release.yml) — there is no second, drifted
# implementation of "the merge gate" anywhere (CICD-27).
verify: lint type cov security responsible todo-gate slo-check citation-check i18n-check external-refs wheel container-verify ## Run the complete merge gate (format+lint + type + test/cov + security + stage-8 guards + todo-gate + slo/citation/i18n schema checks + wheel build + stage-9 container build/CVE-scan/bring-up)
	@echo "verify: all gates green"

clean: ## Remove caches and build artifacts
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache *.egg-info src/*.egg-info htmlcov coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
