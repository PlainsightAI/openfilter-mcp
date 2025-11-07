# Openfilter-MCP

This is an MCP server that allows LLMs access code and information about Plainsight OpenFilter.

By leveraging semantic retrieval capabilities, any COTS LLM can efficiently access relevant information and code snippets from the OPF platform, answering questions or planning to ensure that pipelines and filters are optimized for user requirements.

This project uses [uv]. first, install dependencies;

```uv sync```

Preindex:

```uv run index```

And finally, serve:

```uv run serve```

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
      "args": ["-y", "mcp-remote", "http://localhost:8888/mcp", "--allow-http"]
    }
  }
}
```
</details>


[uv]: https://docs.astral.sh/uv/getting-started/installation/
