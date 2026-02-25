# Token Elicitation Demo — Prompt Script

Server must be running on localhost:3000 before starting.

## Cursor (inline elicitation)

1. "What entity types are available in the Plainsight API?"
2. "List all projects in my organization."
   → approval dialog appears inline → click Approve
3. "What permissions does my current token have?"
4. "Now also list my deployments."
   → clears old token, requests new one with both scopes → approve again
5. "Clear the scoped token."

## Claude Code (browser fallback)

1. "List all projects using the OpenFilter MCP server."
   → agent gets `pending_approval` with a localhost URL
   → open URL in browser → click Approve
2. "What permissions does my current token have?"
3. "I also need to see deployments. Add deployment:read scope."
   → new approval URL → open and approve
4. "Clear the scoped token and show me the token status."
