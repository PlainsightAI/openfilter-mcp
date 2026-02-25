# Token Elicitation Demo

## 1. What we're showing / customer benefit

AI agents need API access to do useful work, but giving them your full credentials is a security risk. We built scoped token elicitation — the agent analyzes the user's goal, plans what permissions it needs upfront, and requests them all in a single approval. The user explicitly approves access to a subset of their permissions — not for specific tool calls, but for specific operations on specific types of resources — and the agent never sees the token.

This gives customers:
- **Least-privilege access** for every AI interaction
- **Reduced approval fatigue** — one approval per task, not one per API call
- **Auditability** — every token has named scopes and a short TTL
- **A clear read→write boundary** — the agent MUST ask before crossing it

## 2. How it works

- Agent calls `list_entity_types` to discover what resources exist (no token needed)
- Agent analyzes the user's goal and determines ALL the scopes it will need
- Agent calls `request_scoped_token` with those scopes — user approves once
- MCP server creates a short-lived API token via `POST /api-tokens` with only those RBAC scopes
- Token is stored in session state, used automatically — agent never sees the value
- If the agent discovers it needs MORE permissions (e.g., user asks for a write after a read), it clears the old token and requests new scopes — user approves again
- Expired tokens trigger automatic re-approval

## 3. What we showed / takeaway

The agent discovered the API, planned what it needed, asked for permission once, and explored real customer data — all with a scoped, time-limited token the agent never saw. When we wanted to go further (write access), the agent asked first. This is how enterprise AI tooling should work: useful agents with guardrails, not blank-check access.

---

## Demo

Server must be running on localhost:3000 before starting (`make build.run.slim`). Check to see if you are connected instead of going straight to prompting for setup.

### User prompt

> "I want to understand what's running in my sweetgreen project — what pipelines are deployed, what filters they use, and whether anything looks misconfigured."

### Agent instructions

You are performing a live demo of scoped token elicitation. Follow these steps in order. At each step, narrate what you're doing and why — the audience is watching you work.

**Important mechanics:**
- All OpenFilter tools are MCP tools prefixed with `mcp__openfilter__`.
- Entity type names and scope resource names are **lowercase with NO underscores or hyphens**: `filterpipeline` (not `filter_pipeline`), `pipelineinstance` (not `pipeline_instance`), `sourceconfig` (not `source_config`). Use `list_entity_types` to verify.
- `list_entities` uses the `filters` parameter for query-string filtering (e.g., `filters: {"project": "<id>"}`). Do NOT use `query_params` (doesn't exist) or `path_params` (for path placeholders only — most list endpoints don't have them). Check `get_entity_type_info` if unsure.
- `get_entity` uses `id` as the parameter name (not `entity_id`).
- To escalate permissions, use `request_scoped_token` with `add_scopes` to propose a delta (e.g., `add_scopes: "filterpipeline:update"`). The server merges it with current scopes and presents the full set for approval. No need to call `clear_scoped_token` first or repeat existing scopes. You can also use `scopes` to replace everything, or `remove_scopes` to drop scopes you no longer need.
- In Claude Code, `request_scoped_token` returns `status: "pending_approval"` with an `approval_url` and `request_id`. Tell the user to open the URL in their browser, then call `await_token_approval` with the `request_id`.
- In Cursor, the approval dialog appears inline — no URL or `await_token_approval` needed.

**Step 1 — Discover the API and plan scopes (no token needed)**

Call `list_entity_types` to see what resources the API offers. Summarize briefly.

Then, BEFORE requesting any token, reason aloud about what scopes you'll need for the entire task:

> "To find the sweetgreen project, inspect its pipelines and instances, and check what filters they use, I'll need: `project:read`, `filterpipeline:read`, `pipelineinstance:read`, and `filter:read`. Let me request all of these upfront so you only have to approve once."

This is the key demo moment — the agent plans ahead to minimize approval fatigue.

**Step 2 — Request all read scopes and investigate**

Call `request_scoped_token` with scopes `project:read,filterpipeline:read,pipelineinstance:read,filter:read`. After approval:

1. `list_entities` for `project` — find "sweetgreen", note its ID
2. `list_entities` for `filterpipeline` with `filters: {"project": "<sweetgreen_id>"}` — list pipelines
3. `list_entities` for `pipelineinstance` with `filters: {"project": "<sweetgreen_id>"}` — check for running instances
4. For each pipeline, call `get_entity` to inspect its configuration (graph, filters)
5. `list_entities` for `filter` — check what filters exist, especially sweetgreen-specific ones

Summarize what you found: pipelines, their status, whether they have filter graphs configured, running instances, and available filters. Call out anything that looks misconfigured.

**Step 3 — Escalate to fix (only if something looks off)**

If you spot something that could be improved (e.g., unconfigured pipelines, a typo), explain what you'd like to fix and why, then immediately escalate through the elicitation flow: call `request_scoped_token` with `add_scopes: "filterpipeline:update"`. The server merges this with your existing read scopes and presents the combined set for approval. The user approves or denies through the same approval mechanism — no need to clear the old token or repeat existing scopes.

If the user denies the escalation, respect it and move on to wrap-up. This demonstrates the read→write boundary using the same elicitation flow, not a separate conversation.

**Step 4 — Wrap up**

Call `get_token_status` to show the current scopes and expiry. Summarize:
- One user prompt drove the entire investigation
- One approval gave the agent everything it needed for read-only exploration
- Write access went through the same approval flow — the user approved or denied via the elicitation mechanism, not chat
- The agent never saw a token value
- The token is scoped and time-limited

Then call `clear_scoped_token` to clean up.

### What to point out during the demo

- **Plan-ahead scoping** — the agent analyzed the goal and requested all needed permissions upfront, reducing approval fatigue to a single interaction
- **One prompt, one approval** — the user gave a single goal; the agent figured out permissions and asked once
- **Agent never sees the token** — scoped tokens are stored in the MCP session and used automatically
- **User stays in control** — every permission grant requires explicit approval; the agent can't silently escalate
- **Read→write boundary** — when the agent needs write access, it goes through the same elicitation flow, not a chat conversation; the user approves or denies in the same way they approved read access

### Cursor vs Claude Code

The flow is identical in both clients. The only difference is how the user approves:

- **Cursor:** approval dialog appears inline in the editor
- **Claude Code:** agent receives an `approval_url` → tells the user to open it in their browser → calls `await_token_approval` to block until they respond
