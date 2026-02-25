# Token Elicitation Demo

Demonstrates how OpenFilter MCP's scoped token system works across two MCP clients:

- **Cursor** — supports inline elicitation (approval dialog appears in the IDE)
- **Claude Code** — does NOT support elicitation; falls back to a browser-based approval page

## What This Demo Shows

When an AI agent needs to call Plainsight API endpoints, it must first request a **scoped token** with only the permissions it needs. The user explicitly approves which scopes the agent gets. The agent never sees the token value — it's stored securely in session state and used automatically for subsequent API calls.

### Flow Overview

```
Agent                          MCP Server                    User
  │                                │                           │
  ├─ request_scoped_token ────────►│                           │
  │   scopes="project:read"        │                           │
  │                                ├─ ctx.elicit() ───────────►│
  │                                │   (inline dialog)         │
  │                                │                           │
  │                                │   ◄── Approve/Deny ──────┤
  │                                │                           │
  │  ◄── {status: "active"} ──────┤   (token created via      │
  │                                │    POST /api-tokens)      │
  │                                │                           │
  ├─ list_entities ───────────────►│   (uses scoped token      │
  │   entity_type="project"        │    automatically)         │
  │  ◄── [project1, project2] ────┤                           │
```

When the client does NOT support `ctx.elicit()` (e.g., Claude Code via `mcp-remote`):

```
Agent                          MCP Server                    User
  │                                │                           │
  ├─ request_scoped_token ────────►│                           │
  │                                ├─ ctx.elicit() FAILS       │
  │                                │   (McpError caught)       │
  │                                │                           │
  │  ◄── {status:                  │                           │
  │       "pending_approval",      │                           │
  │       approval_url: "...",     │                           │
  │       request_id: "..."}  ─────┤                           │
  │                                │                           │
  ├─ "Please open URL" ──────────────────────────────────────►│
  │                                │                           │
  │                                │   ◄── GET /approve/{id} ─┤
  │                                │       (browser opens)     │
  │                                │                           │
  ├─ await_token_approval ────────►│   ◄── POST /respond ─────┤
  │   request_id="..."             │       (clicks Approve)    │
  │                                │                           │
  │  ◄── {status: "active"} ──────┤                           │
```

## Prerequisites

1. **Authenticate with psctl:**
   ```bash
   psctl auth login
   ```

2. **Start the MCP server** (from the repo root):
   ```bash
   # From source:
   uv run serve

   # Or via Docker:
   make build.run.slim
   ```
   The server listens on `http://localhost:3000` by default.

3. **Verify the server is running:**
   ```bash
   curl -s http://localhost:3000/health | jq .
   ```

## Demo 1: Cursor (Inline Elicitation)

Cursor connects to the MCP server via HTTP transport and supports the elicitation protocol natively. The approval dialog appears inline in Cursor's UI.

### Configuration

Copy [`cursor-config.json`](cursor-config.json) to your project's `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "openfilter": {
      "type": "http",
      "url": "http://localhost:3000/mcp"
    }
  }
}
```

### Walkthrough

**Step 1: Discover available entity types**

In Cursor's chat, type:

> What entity types are available in the Plainsight API?

The agent calls `list_entity_types`. You should see a list including `project`, `model`, `training`, `deployment`, `filter_pipeline`, `pipeline_instance`, etc.

**Step 2: Request a scoped token**

Type:

> List all projects in my organization.

The agent realizes it needs API access and calls:
```
request_scoped_token(scopes="project:read", name="openfilter-mcp-agent")
```

**You should see:** An inline approval dialog in Cursor's UI showing:
- Token name: `openfilter-mcp-agent`
- Requested scopes: `project:read`
- Expiration time

Click **"Approve"**.

**Step 3: Agent lists projects**

After approval, the agent receives `{status: "active", scopes: ["project:read"]}` and calls:
```
list_entities(entity_type="project")
```

**You should see:** A list of projects in your organization.

**Step 4: Verify token status**

Type:

> What permissions does my current token have?

The agent calls `get_token_status` and returns:
```json
{
  "status": "scoped",
  "token_name": "openfilter-mcp-agent",
  "scopes": ["project:read"],
  "expires_at": "2026-02-25T21:00:00+00:00"
}
```

**Step 5: Escalate permissions**

Type:

> Now also list my deployments.

The agent realizes it needs `deployment:read` in addition to `project:read`. It calls:
```
clear_scoped_token()
request_scoped_token(scopes="project:read,deployment:read")
```

A new approval dialog appears — click **"Approve"**. The agent then calls `list_entities(entity_type="deployment")`.

## Demo 2: Claude Code (Browser Fallback)

Claude Code connects via `mcp-remote` (stdio-to-HTTP bridge) and does NOT support the elicitation protocol. When the server tries `ctx.elicit()`, it catches the `McpError` and falls back to serving an approval page on the MCP server's HTTP port.

### Configuration

Add to your Claude Code MCP settings (`.claude/settings.json` or `~/.claude.json`):

```json
{
  "mcpServers": {
    "openfilter": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:3000/mcp", "--allow-http"]
    }
  }
}
```

See [`claude-code-config.json`](claude-code-config.json) for the full example.

### Walkthrough

**Step 1: Start Claude Code**

```bash
claude
```

**Step 2: Request projects**

Type:

> List all projects using the OpenFilter MCP server.

The agent calls:
```
request_scoped_token(scopes="project:read")
```

Since Claude Code doesn't support elicitation, the server catches the `McpError` and returns:

```json
{
  "status": "pending_approval",
  "approval_url": "http://localhost:3000/approve/aBcDeFgH",
  "request_id": "xYz12345",
  "message": "Your MCP client does not support interactive approval. Tell the user to open the approval URL in their browser, then call await_token_approval with the request_id."
}
```

**You should see** the agent say something like:

> Please open http://localhost:3000/approve/aBcDeFgH in your browser to approve the token request.

**Step 3: Approve in the browser**

Open the URL. You'll see a Plainsight-branded approval page with:
- Dark navy background (`#0F1020`) with gradient top border
- "Scoped Token Request" title
- A details section listing the token name and requested scopes as turquoise code pills
- **"Approve"** (turquoise) and **"Deny"** (muted) buttons

Click **"Approve"**.

**Step 4: Agent completes the flow**

Back in the terminal, the agent calls:
```
await_token_approval(request_id="xYz12345")
```

This blocks until you click Approve/Deny. Once approved, the server creates the scoped token and returns `{status: "active"}`. The agent then calls `list_entities(entity_type="project")` and shows your projects.

**Step 5: Verify and escalate**

Same as the Cursor demo — use `get_token_status` to check permissions, `clear_scoped_token` + `request_scoped_token` to escalate.

## Demo 3: Token Expiry and Re-Approval

This demo shows the automatic token renewal flow when a scoped token expires mid-session.

### Setup

Request a token with a very short TTL:

> Request a scoped token for project:read that expires in 1 hour, but name it "short-lived-test".

The agent calls:
```
request_scoped_token(scopes="project:read", name="short-lived-test", expires_in_hours=1)
```

> **Note:** The minimum `expires_in_hours` is 1. For a true expiry demo, you'd need to
> manually expire the token via the API or wait for the full hour. In practice, the
> re-approval flow is triggered automatically by `_recreate_expired_token` in
> `entity_tools.py` when any entity CRUD call detects the token has expired.

### What Happens on Expiry

When the scoped token expires and the agent makes an API call:

1. `entity_tools.py` checks the token's `expires_at` metadata before each request.
2. If expired, `_recreate_expired_token` fires — same elicitation → browser fallback pattern.
3. **Cursor**: An inline dialog appears asking "Your scoped token 'short-lived-test' has expired. Re-create with same scopes?"
4. **Claude Code**: A browser approval page opens with the renewal request.
5. On approval, a new token is created with a fresh 1-hour TTL and the original API call proceeds.

## Security Model

- The agent **never sees** the token value — it's stored in MCP session state and used automatically.
- Scoped tokens are created via `POST /api-tokens` on the Plainsight API with specific RBAC scopes.
- The user explicitly approves every token request (and renewal).
- Token values are redacted from all logs and tool responses via `register_sensitive()`.
- Tokens are scoped to the session — clearing the session or the token reverts to no-access (unless `OPF_MCP_ALLOW_UNSCOPED_TOKEN=true` is set for development).

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "No auth token found" | Run `psctl auth login` or set `OPENFILTER_TOKEN` env var |
| Server not starting | Check port 3000 isn't in use: `lsof -i :3000` |
| Approval URL unreachable (Docker) | Ensure you're on a version with the route-based approval fix (commit `6d051db`+). The approval page is served at `/approve/{id}` on the same port as the MCP server. |
| "Insufficient permissions" error | The scoped token doesn't have the right scopes. Use `get_token_status` to check, then `clear_scoped_token` and re-request with additional scopes. |
| Token name conflict (409) | A token with that name already exists. Use a different `name` parameter. |
| `expires_in_hours must be at least 1` | The minimum TTL is 1 hour. |
| Claude Code can't connect | Verify `mcp-remote` is installed: `npx mcp-remote --version`. Check the server URL includes `/mcp` path. |
| Cursor not showing tools | Restart Cursor after modifying `.cursor/mcp.json`. Check the MCP server log for connection events. |

## File Index

| File | Description |
|------|-------------|
| [`README.md`](README.md) | This walkthrough |
| [`cursor-config.json`](cursor-config.json) | Example Cursor MCP configuration |
| [`claude-code-config.json`](claude-code-config.json) | Example Claude Code MCP configuration |
| [`demo-prompts.md`](demo-prompts.md) | Step-by-step prompt crib sheet for running the demo |
