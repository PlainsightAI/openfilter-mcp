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

- Rebuild on the current `python:3.12-slim` base to clear a CRITICAL container-image
  vulnerability flagged by Security Command Center on the deployed image — `libssh2`
  CVE-2026-55200 (CVSS 8.1, exploit available), an OS base-layer package. No application
  changes; a fresh build pulls the patched `libssh2`. (PLAT-1259)

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
