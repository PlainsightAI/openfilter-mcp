# Changelog

openfilter-mcp release notes. Format follows
[Keep a Changelog](https://keepachangelog.com); newest at the top.

New PRs should add bullets under `## [Unreleased]`. When cutting a release,
rename the `[Unreleased]` heading to the new `## vX.Y.Z` and bump
`pyproject.toml` in the same PR (the `auto-tag` workflow keys off
`pyproject.toml` changes on main to push the tag).

## [Unreleased]

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
