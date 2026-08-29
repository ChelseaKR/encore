<!--
Condensed from DEFINITION_OF_DONE.md — that file has the full rationale for each
line. This template exists so the DoD checklist has a delivery vehicle (QM-13):
DEFINITION_OF_DONE.md itself says "the same checklist the PR template enforces,"
and before this file, nothing enforced it.
-->

## What and why

<!-- What changed, and why. Link a related issue/ADR if there is one. -->

## ISO/IEC 25010 characteristic (CQ-42, DEFINITION_OF_DONE.md)

<!-- Name the product-quality characteristic this PR primarily moves, e.g.
     Functional suitability, Reliability, Security, Maintainability,
     Performance efficiency, Compatibility, Usability, Portability. -->

## Checklist

- [ ] `make verify` is green locally (format+lint · type · test/coverage ≥85% ·
      security · todo-gate). `release.yml` runs this literal command at the tag.
      `ci.yml` splits the same targets across jobs and omits `todo-gate` and
      `external-refs`, so green here is stricter than green on the pull request,
      never the reverse. See the CI/CD row in the README.
- [ ] No real Plex token, Apprise URL, or taste data appears anywhere in the diff
      — only synthetic sentinels (`tests/fixtures/`).
- [ ] **No-outing / no-secrets-in-logs** and **read-only-Plex** guards still pass,
      if `src/encore/plex/`, `src/encore/matching/`, or anything logging/serializing
      taste data changed (`pytest -m no_outing -m no_secrets_in_logs -m
      read_only_plex`, merge-blocking from M1). N/A before M1.
- [ ] Accessibility gate is green, if a rendered UI surface changed
      (merge-blocking from M2). N/A before M2.
- [ ] An ADR is added under `docs/adr/` if this PR makes a significant,
      hard-to-reverse decision.
- [ ] `docs/RESPONSIBLE-TECH-AUDITS.md`'s data-inventory table (§C) is updated if
      this PR adds, removes, or changes retention of anything Encore stores; a
      DPIA update (`docs/audits/dpia.md`) accompanies any new outbound integration.
- [ ] `CHANGELOG.md` `[Unreleased]` is updated — user-visible impact, not commit
      subjects.
- [ ] A rollback plan is stated below if this PR changes stored-state shape.

## Rollback plan

<!-- A flag/setting that reverts behavior, or "clean single-commit revert" —
     plus a documented migration-down step if the schema changed. -->
