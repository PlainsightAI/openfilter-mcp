# OpenFilter MCP

An [MCP] server that gives AI agents access to the Plainsight OpenFilter platform — entity management, pipeline deployment, and semantic code search over the OpenFilter monorepo.

## Tools

The server exposes a small, fixed set of tools. The tool count does not grow with the API.

**Platform tools** — 7 generic entity tools that cover the entire Plainsight API. Entity types and schemas are discovered at runtime from the OpenAPI spec via full-text search ([tantivy](https://github.com/quickwit-oss/tantivy)), so agents find what they need without loading hundreds of definitions into context.

**Code search** (optional) — semantic search over indexed OpenFilter repositories. Only available in the full image.

See [docs/tools.md](docs/tools.md) for the full tool reference.

## Quickstart

### Docker (recommended)

```bash
# Authenticate with Plainsight
psctl auth login

# Run the slim image (platform tools only, ~370MB)
docker run -d -p 3000:3000 \
  -v "$(psctl token path):/root/.config/plainsight/token" \
  plainsightai/openfilter-mcp:latest-slim

# Or the full image (platform tools + code search)
docker run -d -p 3000:3000 \
  -v "$(psctl token path):/root/.config/plainsight/token" \
  plainsightai/openfilter-mcp
```

| Tag | Description |
|-----|-------------|
| `latest` / `<version>` | Full build with code search and pre-built indexes |
| `latest-slim` / `<version>-slim` | Platform tools only (no ML dependencies) |

### From source

Requires [uv].

```bash
uv sync          # slim (platform tools only)
uv run serve
```

Or with code search:

```bash
uv sync --group code-search
uv run index
uv run serve
```

### Connect your client

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

<details>
<summary>For clients without HTTP transport support</summary>

```jsonc
{
  "mcpServers": {
    "openfilter": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:3000/mcp", "--allow-http"]
    }
  }
}
```
</details>

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/tools.md](docs/tools.md) | Tool reference and architecture |
| [docs/configuration.md](docs/configuration.md) | Environment variables and authentication |
| [docs/development.md](docs/development.md) | Dev setup, building, testing, and releasing |

[uv]: https://docs.astral.sh/uv/getting-started/installation/
[mcp]: https://anthropic.com/news/model-context-protocol
