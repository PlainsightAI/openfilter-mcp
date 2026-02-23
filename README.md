# Openfilter-MCP

This is a Model Context Protocol ([MCP]) server that allows instruction-optimized AI agents such as large language models (LLMs) or vision language model (VLM) to access code and information about Plainsight OpenFilter.

By leveraging semantic retrieval capabilities, any off-the-shelf LLM or VLM can efficiently access relevant information and code snippets from the OpenFilter platform, answering questions or planning to ensure that pipelines and filters are optimized for user requirements.

## Tools

The server exposes a small, fixed set of tools — the tool count does not grow with the API.

### Platform Tools

Seven generic entity tools cover the entire Plainsight API. Entity types and their schemas are discovered at runtime from the [OpenAPI spec](https://api.prod.plainsight.tech/openapi.json), with two-tier discovery via full-text search (tantivy) so the agent can find relevant entities without loading hundreds of tool definitions into context.

| Tool | Description |
|------|-------------|
| `list_entity_types` | Fuzzy-search available entity types by keyword |
| `get_entity_type_info` | Get schemas, operations, and field details for specific entities |
| `create_entity` | Create an entity (validated against the OpenAPI schema) |
| `get_entity` | Get an entity by ID |
| `list_entities` | List/filter entities with dynamic query parameters |
| `update_entity` | Update an entity |
| `delete_entity` | Delete an entity |
| `entity_action` | Invoke non-CRUD actions (e.g., deploy, start, stop) |

### Code Search Tools (optional)

Semantic search over indexed OpenFilter repositories. Only available in the full image (`latest`), not the slim variant.

| Tool | Description |
|------|-------------|
| `search` | Natural language search for code matching a description |
| `search_code` | Find code similar to a provided snippet |
| `get_chunk` | Retrieve a specific code chunk by ID |
| `read_file` | Read file contents from the indexed monorepo |

This project uses [uv].

### Full install (with code search)

```bash
uv sync --group code-search
uv run index
uv run serve
```

### Slim install (API tools only)

```bash
uv sync
uv run serve
```

The slim variant provides all platform tools but omits code search.

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

## Development

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

## Releasing

Releases are automated via [Google Cloud Build](cloudbuild.yaml). Pushing a `v*` tag triggers a multi-arch (amd64 + arm64) build of both full and slim image variants. Builds are dry-run by default — the Terraform-managed trigger sets `_DRY_RUN=false` to enable pushing.

### Dev builds

Dev tags push to Google Artifact Registry only (not Docker Hub). Tags containing `slim` automatically skip GPU indexing and the full image build.

```bash
make release.dev V=0.2.0        # full + slim → GAR
make release.slim-dev V=0.2.0   # slim only → GAR (much faster)
```

### Production releases

Non-dev tags push to Docker Hub. The `latest` / `latest-slim` tags are automatically updated if the new version is >= the current latest.

```bash
make release.prod V=0.2.0
```

[uv]: https://docs.astral.sh/uv/getting-started/installation/
[mcp]: https://anthropic.com/news/model-context-protocol
