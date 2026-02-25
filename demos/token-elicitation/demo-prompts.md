# Demo Prompts — Token Elicitation

Step-by-step scripts for running the token elicitation demo. Each step lists the exact prompt, the expected tool calls, and what to look for.

---

## Cursor Demo Script

> **Prerequisites:** Server running on `localhost:3000`. Cursor configured with `.cursor/mcp.json` (see [`cursor-config.json`](cursor-config.json)).

### Step 1 — Discover entity types

**Prompt:**
```
What entity types are available in the Plainsight API?
```

**Expected tool calls:**
```
list_entity_types()
```

**You should see:** A list of entity types with descriptions (project, model, training, deployment, filter_pipeline, pipeline_instance, etc.).

---

### Step 2 — Trigger scoped token request

**Prompt:**
```
List all projects in my organization.
```

**Expected tool calls:**
```
request_scoped_token(scopes="project:read", name="openfilter-mcp-agent", expires_in_hours=1)
```

**You should see:**
- An **inline approval dialog** in Cursor showing:
  - Token name: `openfilter-mcp-agent`
  - Scopes: `project:read`
  - Expiration timestamp
- Click **"Approve"**

**After approval:**
```
list_entities(entity_type="project")
```

**You should see:** A list of projects with IDs and names.

---

### Step 3 — Verify token status

**Prompt:**
```
What permissions does my current token have?
```

**Expected tool calls:**
```
get_token_status()
```

**You should see:**
```json
{
  "status": "scoped",
  "token_name": "openfilter-mcp-agent",
  "scopes": ["project:read"],
  "expires_at": "2026-02-25T21:00:00+00:00"
}
```

---

### Step 4 — Escalate permissions

**Prompt:**
```
Now also list my deployments.
```

**Expected tool calls:**
```
clear_scoped_token()
request_scoped_token(scopes="project:read,deployment:read", name="openfilter-mcp-agent")
```

**You should see:**
- A **new approval dialog** with the expanded scopes
- Click **"Approve"**

**After approval:**
```
list_entities(entity_type="deployment")
```

---

### Step 5 — Clean up

**Prompt:**
```
Clear the scoped token.
```

**Expected tool calls:**
```
clear_scoped_token()
```

**You should see:** Confirmation that the scoped token was cleared.

---

## Claude Code Demo Script

> **Prerequisites:** Server running on `localhost:3000`. Claude Code configured with `mcp-remote` (see [`claude-code-config.json`](claude-code-config.json)).

### Step 1 — Start Claude Code

```bash
claude
```

---

### Step 2 — Trigger scoped token (browser fallback)

**Prompt:**
```
List all projects using the OpenFilter MCP server.
```

**Expected tool calls:**
```
request_scoped_token(scopes="project:read")
```

**You should see in the terminal:**
- The agent receives a `pending_approval` response:
  ```json
  {
    "status": "pending_approval",
    "approval_url": "http://localhost:3000/approve/<session_id>",
    "request_id": "<request_id>"
  }
  ```
- The agent tells you: "Please open http://localhost:3000/approve/... in your browser to approve."

**Action:** Open the URL in your browser.

**You should see in the browser:**
- Plainsight-branded dark page with:
  - Gradient top border (turquoise → purple → grape)
  - "Scoped Token Request" heading
  - Token name and scopes displayed as code pills
  - **"Approve"** button (turquoise) and **"Deny"** button (muted)
- Click **"Approve"**

---

### Step 3 — Agent completes the flow

After you click Approve, the agent calls:
```
await_token_approval(request_id="<request_id>")
```

**You should see:**
- `{status: "active", scopes: ["project:read"]}` returned
- Agent then calls `list_entities(entity_type="project")`
- Project list displayed in the terminal

---

### Step 4 — Verify token status

**Prompt:**
```
What permissions does my current token have?
```

**Expected tool calls:**
```
get_token_status()
```

**You should see:** Same output as Cursor Step 3.

---

### Step 5 — Escalate permissions (browser fallback again)

**Prompt:**
```
I also need to see deployments. Add deployment:read scope.
```

**Expected tool calls:**
```
clear_scoped_token()
request_scoped_token(scopes="project:read,deployment:read")
```

**You should see:**
- Another `pending_approval` response with a new URL
- Open the URL → see the expanded scopes → click **"Approve"**
- Agent calls `await_token_approval`, then `list_entities(entity_type="deployment")`

---

### Step 6 — Clean up

**Prompt:**
```
Clear the scoped token and show me the token status.
```

**Expected tool calls:**
```
clear_scoped_token()
get_token_status()
```

**You should see:** Token cleared, status shows "default" (using server token).

---

## Token Expiry Demo Script

> Works in either client. This shows the automatic re-approval flow.

### Step 1 — Create a short-lived token

**Prompt:**
```
Request a scoped token for project:read with the name "expiry-test".
```

**Expected tool calls:**
```
request_scoped_token(scopes="project:read", name="expiry-test", expires_in_hours=1)
```

Approve the request (inline or browser, depending on client).

---

### Step 2 — Use the token

**Prompt:**
```
List projects.
```

**Expected tool calls:**
```
list_entities(entity_type="project")
```

**You should see:** Projects listed successfully.

---

### Step 3 — Wait for expiry, then trigger re-approval

> **Note:** With `expires_in_hours=1`, you'll need to wait 1 hour for natural expiry.
> For a faster demo, the token can be deleted manually via the Plainsight portal,
> which will cause subsequent API calls to fail and trigger the renewal flow.

**Prompt (after expiry):**
```
List projects again.
```

**What happens internally:**
1. `entity_tools.py` checks token metadata, finds it expired
2. `_recreate_expired_token` fires
3. **Cursor:** Inline dialog — "Your scoped token 'expiry-test' has expired. Re-create with same scopes?"
4. **Claude Code:** Browser page opens with renewal request
5. On approval, new token created, original request retried

**You should see:** A brief pause, then re-approval prompt, then projects listed.

---

## Tips for a Smooth Demo

- **Use unique token names** if re-running — the API returns 409 on duplicate names. Add a suffix like `-demo1`, `-demo2`.
- **Watch the server logs** (`uv run serve` output) to see token creation events in real time.
- **Keep a browser tab ready** for Claude Code demos — you'll need to open the approval URL quickly.
- **The agent may call `list_entity_types` first** before requesting a token — this is expected behavior (discovery before action).
- **If the agent doesn't request a token**, remind it: "You need to request a scoped token first."
