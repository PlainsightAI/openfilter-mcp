# Changelog

openfilter-mcp release notes. Format follows
[Keep a Changelog](https://keepachangelog.com); newest at the top.

New PRs should add bullets under `## [Unreleased]`. When cutting a release,
rename the `[Unreleased]` heading to the new `## vX.Y.Z` and bump
`pyproject.toml` in the same PR (the `auto-tag` workflow keys off
`pyproject.toml` changes on main to push the tag).

## [Unreleased]

## v0.2.4

### Security

- Clear a CRITICAL container-image vulnerability flagged by Security Command Center —
  `libssh2` CVE-2026-55200 (CVSS 8.1, exploit available), an OS base-layer package.
  All published images are rebuilt to carry the fixed `libssh2` deterministically:
  the slim image (`Dockerfile.slim`) and `Dockerfile` explicitly upgrade `libssh2-1t64`
  on `python:3.12-slim`; the full/`:latest` image (`Dockerfile.gpu`) moves its runtime
  stage from `debian:bookworm-slim` (no fixed build) to `debian:trixie-slim`, where the
  fix ships as `libssh2-1t64` 1.11.1-1+deb13u1 (DSA-6365-1). Also bumps `uv.lock` to
  `0.2.4` so `uv sync --locked` stays reproducible. No application changes. (PLAT-1259)

### Fixed

- Startup no longer silently degrades to a token-tools-only catalog when the
  OpenAPI fetch fails. The fetch now retries with capped exponential backoff
  (absorbing the DNS cold-start race at pod startup), fails fast under
  `REQUIRE_AUTH` rather than serving an unusable catalog, and a new
  unauthenticated `/health` endpoint returns 503 until entity tools register
  so the readiness probe keeps a degraded pod out of rotation.

## v0.2.3

### Added

- `check-release-log` gate on every PR to `main` (enforces this changelog is
  updated for substantive changes; scoped via `ignore-paths` so docs / tests
  / CI / deploy-config-only PRs are no-ops).
- `external-review-freshness` workflow: dismisses approvals on PRs authored
  by untrusted contributors whenever they push new commits, so any new code
  from a non-collaborator forces re-review.

### Fixed

- Auth now accepts both trailing-slash and non-trailing-slash token issuers
  via syntax-aware URL parsing, and tolerates trailing-slash audience values.

## v0.2.2

- Initial tracked release. Prior history is captured in git only.
