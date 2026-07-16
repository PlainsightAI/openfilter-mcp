# Changelog

openfilter-mcp release notes. Format follows
[Keep a Changelog](https://keepachangelog.com); newest at the top.

New PRs should add bullets under `## [Unreleased]`. When cutting a release,
rename the `[Unreleased]` heading to the new `## vX.Y.Z` and bump
`pyproject.toml` in the same PR (the `auto-tag` workflow keys off
`pyproject.toml` changes on main to push the tag).

## [Unreleased]

## v0.2.6

### Fixed

- Slim image no longer crash-loops at startup. v0.2.5 dropped `git` from the slim
  image, but `openfilter_mcp.server` imports GitPython (`preindex_repos`) at module
  load, and GitPython raises `ImportError: Bad git executable` when the binary is
  absent — even with code search off, where git is never actually invoked. Set
  `GIT_PYTHON_REFRESH=quiet` so GitPython defers its executable check and the import
  succeeds. Keeps the v0.2.5 security win (no `git`/`perl`/`libssh2` in the image).

## v0.2.5

### Security

- The slim image (`Dockerfile.slim`) now installs **no OS packages**, clearing
  three CRITICAL Security Command Center findings by removal. It previously pulled
  in `git` and `curl` but needs neither: the slim build excludes the `code-search`
  group (the sole git-sourced dep, `llama-cpp-python`), so `uv sync` needs no `git`,
  and the server talks to plainsight-api over `httpx` — not `curl` — with
  `httpGet`/`tcpSocket` probes. Dropping them removes `git`'s transitive `perl`
  (CVE-2026-42496 CVSS 9.1, CVE-2026-8376 CVSS 7.3 — **no upstream Debian trixie
  fix**, so removal is the only lever, unlike the v0.2.4 libssh2 upgrade) and
  `curl`'s transitive `libssh2` (CVE-2026-55200, superseding v0.2.4's explicit
  upgrade for this image). Only essential `perl-base` remains (not flagged), and the
  image shrinks. The full/`:latest` and default images keep `git` (used at runtime
  for code-search clones) and are unaffected. (PLAT-1259)

### Changed

- `publish-chart` CI now renders the Helm chart against each environment's overrides
  (`development`/`staging`/`production`) instead of base values. The chart requires
  per-env values (`plainsightApiUrl`, via the `openfilter-mcp.validate` helper), so
  the old base-values `helm template` failed on every tag — aborting the chart
  publish. Rendering per-env fixes that and validates all three environments.

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
