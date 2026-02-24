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

**Precedence**: `PS_API_URL` > `PSCTL_API_URL` > `PLAINSIGHT_API_URL` > default
