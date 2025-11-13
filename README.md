# Openfilter-MCP

This is a Model Context Protocol ([MCP]) server that allows instruction-optimized AI agents such as large language models (LLMs) or vision language model (VLM) to access code and information about Plainsight OpenFilter.

By leveraging semantic retrieval capabilities, any off-the-shelf LLM or VLM can efficiently access relevant information and code snippets from the OpenFilter platform, answering questions or planning to ensure that pipelines and filters are optimized for user requirements.

This project uses [uv]. first, install dependencies;

```uv sync```

Preindex:

```uv run index```

And finally, serve:

```uv run serve```

You can also run the version published on DockerHub:

```
docker run 
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
      "args": ["-y", "mcp-remote", "http://localhost:8888/mcp", "--allow-http"]
    }
  }
}
```
</details>


[uv]: https://docs.astral.sh/uv/getting-started/installation/
[mcp]: https://anthropic.com/news/model-context-protocol
