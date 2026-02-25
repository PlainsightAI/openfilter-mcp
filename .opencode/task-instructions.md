# Task: Token Elicitation Demo Scripts

Implement demo scripts and documentation that showcase the OpenFilter MCP token elicitation flow in two MCP clients: **Cursor** and **Claude Code (CLI)**. Commit and open a PR.

## Background

OpenFilter MCP implements a scoped token system where AI agents must request limited-permission API tokens before making API calls. The server uses the MCP elicitation protocol (`ctx.elicit()`) to present an approval dialog to the user. When the client doesn't support elicitation (e.g., Claude Code), it falls back to a browser-based approval page served on localhost.

This is a key security feature — the agent never sees the token value, and the user explicitly approves what permissions the agent gets. We need a scripted demo showing both flows end-to-end, suitable for internal demos and documentation.

### How the flow works

1. **Agent calls `request_scoped_token`** with scopes like `"project:read,deployment:read"`.
2. **Server tries `ctx.elicit()`** — if the client supports it (e.g., Cursor), an inline approval dialog appears.
3. **If elicitation fails** (client doesn't support it, e.g., Claude Code via `mcp-remote`), the server starts a localhost HTTP server (`approval_server.py`) and returns `{"status": "pending_approval", "approval_url": "http://127.0.0.1:<port>/", "request_id": "..."}`.
4. **Agent tells user to open the URL**, then calls `await_token_approval(request_id)` which blocks until the user clicks Approve/Deny.
5. **On approval**, the server creates a scoped `ps_` API token via `POST /api-tokens` and stores it in session state. All subsequent entity CRUD calls use this token automatically.
6. **Token expiry**: When a scoped token expires mid-session, `_recreate_expired_token` in `entity_tools.py` triggers a re-approval flow (same elicitation → browser fallback pattern).

### Key files

- `src/openfilter_mcp/server.py` — `request_scoped_token`, `await_token_approval`, `get_token_status`, `clear_scoped_token` tools; `_create_and_activate_token` helper.
- `src/openfilter_mcp/entity_tools.py` — `EntityToolsHandler._get_request_headers` (reads scoped token from session state), `_recreate_expired_token` (re-approval on expiry).
- `src/openfilter_mcp/approval_server.py` — Localhost HTTP server with Plainsight-branded dark-mode approval page. `ApprovalSession` with `.url` and `.wait()`.
- `src/openfilter_mcp/auth.py` — `get_auth_token()` (reads `OPENFILTER_TOKEN` env or psctl token file), `create_token_verifier()`.

### Server configuration

```bash
# Run server from source (slim mode, no code search)
uv run serve
# Server listens on http://localhost:3000

# Environment variables:
#   OPENFILTER_TOKEN or psctl token file — base auth token
#   OPF_MCP_ALLOW_UNSCOPED_TOKEN=true — skip scoped token requirement (for testing)
#   ENABLE_CODE_SEARCH=false — disable code search tools
```

### Client configurations

**Cursor** (supports elicitation natively via HTTP transport):
```jsonc
// .cursor/mcp.json
{
  "mcpServers": {
    "openfilter": {
      "type": "http",
      "url": "http://localhost:3000/mcp"
    }
  }
}
```

**Claude Code** (does NOT support elicitation — triggers browser fallback):
```jsonc
// .claude/settings.json or ~/.claude.json
{
  "mcpServers": {
    "openfilter": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:3000/mcp", "--allow-http"]
    }
  }
}
```

## Implementation

### Files to create

#### 1. `demos/token-elicitation/README.md`

A walkthrough document structured as:

1. **Overview** — What this demo shows (scoped token elicitation in two clients).
2. **Prerequisites** — `psctl auth login`, `uv run serve`, running server on port 3000.
3. **Demo 1: Cursor (inline elicitation)**
   - Show how to configure `.cursor/mcp.json`.
   - Script: Ask the agent "List all projects in my organization." The agent should:
     - Call `list_entity_types` to discover entity types.
     - Call `request_scoped_token` with `"project:read"`.
     - An inline approval dialog appears in Cursor's UI — user clicks "Approve".
     - Agent receives confirmation, then calls `list_entities` for projects.
   - Include expected screenshots/output descriptions at each step.
   - Show `get_token_status` to verify scoped permissions.
   - Show `clear_scoped_token` to reset.

4. **Demo 2: Claude Code (browser fallback)**
   - Show how to configure Claude Code MCP settings (uses `mcp-remote` bridge).
   - Same script: "List all projects in my organization."
   - The agent calls `request_scoped_token` — elicitation fails → browser fallback.
   - Agent receives `{"status": "pending_approval", "approval_url": "http://127.0.0.1:XXXXX/"}`.
   - Agent tells user: "Please open http://127.0.0.1:XXXXX/ to approve."
   - User opens URL → sees Plainsight-branded approval page → clicks "Approve".
   - Agent calls `await_token_approval(request_id)` → receives confirmation.
   - Agent proceeds with `list_entities`.
   - Include expected terminal output at each step.

5. **Demo 3: Token expiry and re-approval** (bonus, either client)
   - Set `expires_in_hours=0.01` (36 seconds) on `request_scoped_token`.
   - Wait for token to expire, then make another API call.
   - Show the automatic re-approval flow triggered by `_recreate_expired_token`.

6. **Troubleshooting** — Common issues (no token, server not running, port conflict, etc.)

#### 2. `demos/token-elicitation/cursor-config.json`

Example `.cursor/mcp.json` configuration:
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

#### 3. `demos/token-elicitation/claude-code-config.json`

Example Claude Code MCP configuration:
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

#### 4. `demos/token-elicitation/demo-prompts.md`

A crib sheet of exact prompts to use during the demo, with expected agent behavior and tool calls at each step. Structured as a numbered sequence:

```
## Cursor Demo Script

1. Open Cursor with the project that has .cursor/mcp.json configured.
2. In the chat, type: "What entity types are available in the Plainsight API?"
   → Agent calls `list_entity_types` → shows available entities.
3. Type: "List all projects in my organization."
   → Agent calls `request_scoped_token(scopes="project:read")`
   → Inline elicitation dialog appears → Click "Approve"
   → Agent calls `list_entities(entity_type="project")`
   → Shows project list.
4. Type: "What permissions does my current token have?"
   → Agent calls `get_token_status` → shows scoped permissions.
5. Type: "Now also list my deployments."
   → Agent should realize it needs `deployment:read` scope.
   → Calls `clear_scoped_token`, then `request_scoped_token(scopes="project:read,deployment:read")`
   → New approval dialog → Approve → Lists deployments.

## Claude Code Demo Script

1. Start Claude Code: `claude`
2. Type: "List all projects using the OpenFilter MCP server."
   → Agent calls `request_scoped_token(scopes="project:read")`
   → Gets `pending_approval` response with URL.
   → Agent says: "Please open http://127.0.0.1:XXXXX/ to approve the token."
   → Open URL in browser → Plainsight-branded page → Click "Approve"
   → Agent calls `await_token_approval(request_id="...")`
   → Gets `active` response.
   → Agent calls `list_entities(entity_type="project")`
   → Shows project list.
3. (Continue with same escalation flow as Cursor script)
```

### Notes for implementation

- The README should be written in a "run the demo" style — concise, action-oriented, with clear "you should see" callouts.
- Use fenced code blocks for all terminal output and JSON.
- Reference the actual tool names from `server.py`: `request_scoped_token`, `await_token_approval`, `get_token_status`, `clear_scoped_token`, `list_entity_types`, `list_entities`, `create_entity`, etc.
- The approval page screenshot description should mention: dark navy background, gradient top border, turquoise/purple branding, "Approve" and "Deny" buttons, scopes listed in a table.
- Do NOT include any actual tokens or secrets in the demo files.

## Verification

1. All markdown files render correctly (no broken links or formatting).
2. The JSON config files are valid JSON.
3. The demo prompts reference real tool names that exist in `server.py`.
4. The flow descriptions accurately match the code in `server.py` (elicitation → McpError catch → browser fallback).

## Cross-repo context

This is a single-repo task within `openfilter-mcp`. The token elicitation feature is already merged to `main` (from `feat/ti-343-token-elicitation`). This demo documents the existing functionality.

Originating branch: `main` at commit `fbb87f2`.
