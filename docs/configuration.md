# Configuration

## Authentication

The MCP server uses the same authentication as `psctl`. After running `psctl auth login`, the server automatically uses your stored credentials from `~/.config/plainsight/token` (or `$XDG_CONFIG_HOME/plainsight/token` if set).

When running in Docker, mount the token file into the container:

```bash
docker run -d -p 3000:3000 \
  -v "$(psctl token path):/root/.config/plainsight/token" \
  plainsightai/openfilter-mcp
```

The mount is read-write so the server can refresh expired tokens automatically.

> **Note**: Run `psctl auth login` before using `psctl token path` in scripts or CI. If no token exists, `psctl token path` will prompt interactively, which hangs in non-interactive environments.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PS_API_URL` | Plainsight API URL (same as psctl) | `https://api.prod.plainsight.tech` |
| `PSCTL_API_URL` | Alternative psctl-style API URL | `https://api.prod.plainsight.tech` |
| `PLAINSIGHT_API_URL` | Legacy fallback | `https://api.prod.plainsight.tech` |
| `ENABLE_CODE_SEARCH` | Set to `false` to disable code search even if indexes are present | `true` |
| `MCP_BASE_URL` | Base URL prefix for the browser approval redirects/links (e.g., `https://mcp.dev.plainsight.tech`). Falls back to `OAUTH_RESOURCE_URL` if configured. | `http://localhost:{PORT}` (defaults to `3000`) |
| `OAUTH_RESOURCE_URL` | The public/external URL of this MCP server, used for OAuth audience mapping and browser approval redirects. | `http://localhost:{PORT}` (defaults to `3000`) |
| `OAUTH_AS_URL` | The base URL of the OAuth authorization server. Setting this activates OAuth-only mode. | None |
| `OAUTH_AUDIENCE` | Custom comma-separated list of OAuth audience values. | `f"{OAUTH_RESOURCE_URL}/mcp, OAUTH_AS_URL"` |
| `PORT` | The HTTP port the MCP server and approval router listen on. | `3000` |
| `REQUIRE_AUTH` | Force-require a valid startup token or OAUTH_AS_URL configuration to start the server. | `false` |
| `OPF_MCP_ALLOW_UNSCOPED_TOKEN` | Set to `true` to bypass the scoped-token validation gate for testing/local development. | `false` |

**Precedence**: `PS_API_URL` > `PSCTL_API_URL` > `PLAINSIGHT_API_URL` > default

## Troubleshooting (validated on 2026-08 client testing)

- **The `:full` image crashes at startup without a GPU** (`libcuda.so.1: cannot open shared object file`, raised from the llama_cpp import). Only that tag links CUDA: it is built from `Dockerfile.gpu`, which compiles llama-cpp-python against CUDA for code search. `Dockerfile.slim` builds the `:slim` tag and the published GAR image, and the repository's root `Dockerfile` is CPU-only as well (`CMAKE_ARGS=-DGGML_CUDA=off`), so reading either of those does not explain the crash. On machines without an NVIDIA GPU (laptops, CI) use `-slim`, which is also what the hosted deployments run.
- **"REQUIRE_AUTH is set but no authentication path is available" with a mounted token**: the mounted psctl token is expired and its refresh token is no longer valid. Run `psctl auth login` again; a stale token file (check its mtime) is the usual cause.
- **Headless/scripted clients and scoped tokens**: the scoped token binds to the MCP session that calls `await_token_approval`, and the approval request is consumed by it. One-shot CLI invocations (a new session per call) cannot reuse an approved token; run the whole flow in a single session, or set `OPF_MCP_ALLOW_UNSCOPED_TOKEN=true` for scripted testing.
- **Gemini CLI headless**: needs `GEMINI_CLI_TRUST_WORKSPACE=true` and `--allowed-mcp-server-names openfilter` in addition to the `mcpServers` settings entry (see `docs/demo/.gemini/settings.json`).
