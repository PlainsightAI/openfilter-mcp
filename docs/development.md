# Development

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Docker (for building images)

## Setup

```bash
uv sync --group dev
```

## Make targets

Run `make help` to see all available targets:

```
test                   Run tests
build.slim             Build slim Docker image (no ML deps, ~370MB)
build.full             Build full Docker image (requires indexes/)
build.run.slim         Build and run slim image
build.run.full         Build and run full image
index                  Build code search indexes from source
index.extract          Extract indexes from published amd64 image
release.dev            Tag and push a dev build (full + slim → GAR)
release.slim-dev       Tag and push a slim-only dev build (→ GAR)
release.prod           Tag and push a production release (→ Docker Hub)
```

## Testing

```bash
make test
```

## Project structure

```
src/openfilter_mcp/
├── server.py           # MCP server entry point, code search tools
├── entity_tools.py     # Generic entity CRUD tools + tantivy discovery
├── auth.py             # psctl token-based authentication
└── preindex_repos.py   # Code search index builder

code-context/           # AST-aware code chunking library (workspace member)
scripts/
└── docker-build.sh     # Shared Docker build logic for Cloud Build
```

### Dependency groups

| Group | What it adds | When to use |
|-------|-------------|-------------|
| _(base)_ | Platform tools, tantivy, inflect | `uv sync` — slim installs |
| `code-search` | code-context, torch, llama-cpp-python, faiss | `uv sync --group code-search` — full installs |
| `dev` | pytest, httpx | `uv sync --group dev` — development |

## Releasing

Releases are automated via [Google Cloud Build](../cloudbuild.yaml). Pushing a `v*` tag triggers a multi-arch (amd64 + arm64) build of both full and slim image variants. Builds are dry-run by default — the Terraform-managed trigger sets `_DRY_RUN=false` to enable pushing.

### Dev builds

Dev tags push to Google Artifact Registry only (not Docker Hub). Tags containing `slim` skip GPU indexing and the full image build.

```bash
make release.dev V=0.2.0        # full + slim → GAR
make release.slim-dev V=0.2.0   # slim only → GAR (much faster)
```

### Production releases

Non-dev tags push to Docker Hub. `latest` / `latest-slim` are updated if the new version >= current latest.

```bash
make release.prod V=0.2.0
```
