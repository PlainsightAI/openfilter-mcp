# Token Elicitation Demo

## 1. What we're showing / customer benefit

AI agents need API access to do useful work, but giving them your full credentials is a security risk. We built scoped token elicitation — the agent requests only the permissions it needs, the user explicitly approves, and the agent never sees the token. This gives customers auditability and least-privilege access for every AI interaction with their Plainsight resources.

## 2. How it works

- Agent calls `request_scoped_token` with specific scopes (e.g. `project:read`)
- MCP server presents an approval dialog to the user — inline in Cursor, browser-based in Claude Code
- On approval, server creates a short-lived API token via `POST /api-tokens` with only those RBAC scopes
- Token is stored in session state, used automatically — agent never sees the value
- If the agent needs more permissions later, it clears the old token and requests new scopes — user approves again
- Expired tokens trigger automatic re-approval

## 3. What we showed / takeaway

The agent discovered the API, asked for permission, got approval, and listed real customer data — all with a scoped, time-limited token the agent never saw. The user stayed in control at every step. This is how enterprise AI tooling should work: useful agents with guardrails, not blank-check access.

---

## Demo prompts

Server must be running on localhost:3000 before starting.

### Cursor (inline elicitation)

1. "What entity types are available in the Plainsight API?"
2. "List all projects in my organization."
   → approval dialog appears inline → click Approve
3. "What permissions does my current token have?"
4. "Now also list my deployments."
   → clears old token, requests new one with both scopes → approve again
5. "Clear the scoped token."

### Claude Code (browser fallback)

1. "List all projects using the OpenFilter MCP server."
   → agent gets `pending_approval` with a localhost URL
   → open URL in browser → click Approve
2. "What permissions does my current token have?"
3. "I also need to see deployments. Add deployment:read scope."
   → new approval URL → open and approve
4. "Clear the scoped token and show me the token status."
