# Openfilter-MCP

This is a Model Context Protocol ([MCP]) server that allows instruction-optimized AI agents such as large language models (LLMs) or vision language model (VLM) to access code and information about Plainsight OpenFilter.

By leveraging semantic retrieval capabilities, any off-the-shelf LLM or VLM can efficiently access relevant information and code snippets from the OpenFilter platform, answering questions or planning to ensure that pipelines and filters are optimized for user requirements.

## Tools

The MCP server exposes two categories of tools:

### Plainsight API Tools (Auto-generated)

All Plainsight API endpoints are automatically exposed as MCP tools via [FastMCP's OpenAPI integration][fastmcp-openapi]. This includes:

- **Projects & Organizations**: List, create, and manage projects
- **Video Corpus**: Upload, list, and manage videos
- **Filter Pipelines**: Configure and deploy filter pipelines
- **Test Management**: Create tests with assertions and golden truth files
- **Synthetic Video Generation**: Generate synthetic test videos via AI
- **And more...**: All API endpoints documented in the [OpenAPI spec](https://api.prod.plainsight.tech/openapi.json)

### Code Search Tools (Manual)

These tools provide semantic code search capabilities on indexed repositories:

- `search`: Natural language search for code matching a description
- `search_code`: Find code similar to a provided snippet
- `get_chunk`: Retrieve a specific code chunk by ID
- `read_file`: Read file contents from the indexed monorepo

[fastmcp-openapi]: https://gofastmcp.com/integrations/openapi

This project uses [uv].

### Full install (with code search)

Install dependencies including semantic search:

```bash
uv sync --extra code-search
```

Preindex:

```bash
uv run index
```

And finally, serve:

```bash
uv run serve
```

### Slim install (API tools only)

For a lightweight install without semantic code search, embedding models, or ML dependencies:

```bash
uv sync
uv run serve
```

The slim variant provides all Plainsight API tools (entity CRUD, polling, etc.) but omits code search tools (`search`, `search_code`, `get_chunk`, `read_file`).

## Docker

Both full and slim image variants are published to [Docker Hub](https://hub.docker.com/r/plainsightai/openfilter-mcp):

| Tag | Description |
|-----|-------------|
| `latest` / `<version>` | Full build with semantic code search and pre-built indexes |
| `latest-slim` / `<version>-slim` | Slim build with API tools only (no ML dependencies) |

```bash
# Run the full container (with code search)
docker run --name openfilter-mcp -d -p 3000:3000 plainsightai/openfilter-mcp

# Run the slim container (API tools only, much smaller image)
docker run --name openfilter-mcp -d -p 3000:3000 plainsightai/openfilter-mcp:latest-slim

# Check logs
docker logs openfilter-mcp

# Stop the container
docker stop openfilter-mcp
```

### Authentication with Docker

To use Plainsight API tools (project management, video corpus, filter pipelines, etc.), you need to mount your local token file into the container.

First, authenticate using the psctl CLI on your host machine:

```bash
# Login first (required before running in scripts/CI)
psctl auth login

# Verify the token path
psctl token path
# Output: /home/user/.config/plainsight/token (or similar)
```

> **Important**: You must run `psctl auth login` before using `psctl token path` in scripts or non-interactive environments. If no token exists, `psctl token path` will prompt for login interactively, which will hang in automated/non-interactive contexts.

Then mount the token file when running the container:

```bash
# Get the token path and mount it to the container
docker run --name openfilter-mcp -d -p 3000:3000 \
  -v "$(psctl token path):/root/.config/plainsight/token" \
  plainsightai/openfilter-mcp
```

The token file must be mounted to `~/.config/plainsight/token` inside the container (which is `/root/.config/plainsight/token` for the root user). The mount is read-write to allow the MCP server to automatically refresh the token when it expires.

The server will be available at:

```
http://localhost:3000/mcp
```

The configuring MCP client should then add the MCP server under the HTTP transport to the
required url, e.g.:

```jsonc
{
  "mcpServers": {
    "openfilter": {
      "type": "http",
      "url": "http://localhost:3000/mcp"
    }
  }
}
```

## Configuration

### Environment Variables

The following environment variables can be used to configure the MCP server:

| Variable | Description | Default |
|----------|-------------|---------|
| `PS_API_URL` | API URL for Plainsight API (same as psctl CLI) | `https://api.prod.plainsight.tech` |
| `PSCTL_API_URL` | Alternative psctl-style API URL | `https://api.prod.plainsight.tech` |
| `PLAINSIGHT_API_URL` | Legacy API URL (fallback for backwards compatibility) | `https://api.prod.plainsight.tech` |

**Precedence order:** `PS_API_URL` > `PSCTL_API_URL` > `PLAINSIGHT_API_URL` > default

This follows the same convention as the `psctl` CLI for consistency.

### Authentication

The MCP server uses the same authentication as `psctl`. After running `psctl login`, the MCP server will automatically use your stored credentials from `~/.config/plainsight/token` (or `$XDG_CONFIG_HOME/plainsight/token` if set).

<details>
<summary>or, for clients without explicit HTTP support:</summary>

```jsonc
{
  "mcpServers": {
    "openfilter": {
      "command": "npx", // or equivalent, e.g., `pnpm dlx`
      "args": ["-y", "mcp-remote", "http://localhost:3000/mcp", "--allow-http"]
    }
  }
}
```
</details>

## Releasing

Releases are automated via [Google Cloud Build](cloudbuild.yaml). Pushing a `v*` tag triggers a multi-arch (amd64 + arm64) build of both full and slim image variants. Builds are dry-run by default (images built but not pushed) — the Terraform-managed trigger sets `_DRY_RUN=false` to enable pushing.

### Dev builds (testing)

Dev tags push to Google Artifact Registry only (not Docker Hub).

```bash
# Full + slim build
git tag v0.2.0-dev && git push origin v0.2.0-dev

# Slim-only build (skips GPU indexing, much faster)
git tag v0.2.0-slim-dev && git push origin v0.2.0-slim-dev
```

Tags containing `slim` automatically skip the GPU index build and full image build. Both `v*-slim-dev` and `v*-dev-slim` are supported and normalize to the same image tags.

### Production releases

Non-dev tags push to Docker Hub. The `latest` / `latest-slim` tags are automatically updated if the new version is >= the current latest (semver comparison).

```bash
git tag v0.2.0 && git push origin v0.2.0
```

### Local testing

The shared build script can be run locally (dry-run by default — builds but does not push):

```bash
# Build slim image locally
docker build -f Dockerfile.slim -t openfilter-mcp:slim .

# Test the full build script (requires Docker + buildx)
bash scripts/docker-build.sh --dockerfile Dockerfile.slim --tag-suffix "-slim" --latest-tag "latest-slim"
```

[uv]: https://docs.astral.sh/uv/getting-started/installation/
[mcp]: https://anthropic.com/news/model-context-protocol
