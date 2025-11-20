# `code-context`

This is an MCP server that grants LLMs access to arbitrary local code for vibecoding using similar intelligent chunking functionality to [claude-context], with one important difference: [jina-code-embeddings-1.5b], and specifically its [8-bit GGUF quantization][GGUF], are hard-coded in place of remote embedding servers to enable `code2code` search and so that all indexing and search are carried out locally, limiting the exposure of proprietary code to the specific fragments required by external agents where applicable and cutting down on infrastructure management overhead and fees. 

This project uses [uv].

running the server:
`uv run serve`

configuring mcp should then add the MCP server under the HTTP transport to the
required url, e.g.:

```jsonc
{
  "mcpServers": {
    "code-context": {
      "type": "http",
      "url": "http://localhost:8888/mcp"
    }
  }
}
```

<details>
<summary>or, for clients without explicit HTTP support:</summary>

```jsonc
{
  "mcpServers": {
    "code-context": {
      "command": "npx", // or equivalent, e.g., `pnpm dlx`
      "args": ["-y", "mcp-remote", "http://localhost:8888/mcp", "--allow-http"]
    }
  }
}
```
</details>


[uv]: https://docs.astral.sh/uv/getting-started/installation/
[claude-context]: https://github.com/zilliztech/claude-context
[jina-code-embeddings-1.5b]: https://huggingface.co/jinaai/jina-code-embeddings-1.5b
[GGUF]: https://huggingface.co/jinaai/jina-code-embeddings-1.5b-GGUF
