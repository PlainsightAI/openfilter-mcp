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

This project uses [uv]. first, install dependencies;

```uv sync```

Preindex:

```uv run index```

And finally, serve:

```uv run serve```

## Docker

You can build and run this with Docker:

```bash
# Run the container
docker run --name openfilter-mcp -d -p 3000:3000 plainsightai/openfilter-mcp

# Check logs
docker logs openfilter-mcp

# Stop the container
docker stop openfilter-mcp
```

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


[uv]: https://docs.astral.sh/uv/getting-started/installation/
[mcp]: https://anthropic.com/news/model-context-protocol
