# Tools

## Platform Tools

Seven generic entity tools cover the entire Plainsight API. Entity types and their schemas are discovered at runtime from the [OpenAPI spec](https://api.prod.plainsight.tech/openapi.json), with two-tier discovery via full-text search (tantivy) so the agent can find relevant entities without loading hundreds of tool definitions into context.

| Tool | Description |
|------|-------------|
| `list_entity_types` | Fuzzy-search available entity types by keyword |
| `get_entity_type_info` | Get schemas, operations, and field details for specific entities |
| `create_entity` | Create an entity (validated against the OpenAPI schema) |
| `get_entity` | Get an entity by ID |
| `list_entities` | List/filter entities with dynamic query parameters |
| `update_entity` | Update an entity |
| `delete_entity` | Delete an entity |
| `entity_action` | Invoke non-CRUD actions (e.g., deploy, start, stop) |

### How discovery works

Early versions used FastMCP's OpenAPI autodiscovery, which generated 120+ individual tools from the spec. This caused context bloat and degraded model performance — LLMs don't reason well when presented with hundreds of tool definitions.

The current design uses a two-tier approach:

1. **`list_entity_types`** — the agent searches for entity types by keyword (e.g., "pipeline", "video"). Results come from a tantivy full-text index built at startup from the OpenAPI spec. This is cheap context-wise — the agent only sees names and summaries.

2. **`get_entity_type_info`** — once the agent knows which entity types it needs, it fetches full schemas, available operations, and field details on demand.

This keeps the core tool count at 7 regardless of how many API endpoints exist.

## Token Scoping Tools

Before making API calls, the agent requests a scoped token with permissions tailored to the task. The user approves once per scope set, and the token is stored in the MCP session — the agent never sees the token value.

| Tool | Description |
|------|-------------|
| `request_scoped_token` | Request a scoped API token (absolute scopes, or delta via `add_scopes`/`remove_scopes`) |
| `await_token_approval` | Block until browser-based approval completes (Claude Code flow) |
| `get_token_status` | Check current scopes, expiry, and token state |
| `clear_scoped_token` | Revoke the scoped token and revert to default credentials |

### Choosing scope breadth

There is an inherent tension between **least privilege** (narrow scopes reduce blast radius) and **agent autonomy** (broad scopes reduce approval fatigue and let the agent complete work without interruption). Neither extreme is ideal:

| Task profile | Recommended approach | Example scopes |
|---|---|---|
| Quick, focused query | Narrow — only the specific resources and actions needed | `filterpipeline:read,pipelineinstance:read` |
| Broad investigation or long session | Wide read scopes so the agent can explore freely | `*:read` or a broad set of `<resource>:read` |
| Task that clearly implies writes | Read + write for the relevant resources upfront | `filterpipeline:read,filterpipeline:update` |
| Open-ended "fix everything" task | Wide reads, targeted writes — escalate via `add_scopes` only if new resource types arise | `*:read,filterpipeline:update` |

**Guidelines:**
- Aim for the scope set that lets the agent complete the user's stated goal with **a single approval**.
- Reads are low-risk; lean toward broader read scopes when the task is exploratory.
- Writes deserve more scrutiny; keep write scopes targeted unless the user's intent clearly requires broad modifications.
- Avoid `*:*` (full wildcard) unless the task genuinely requires unrestricted access.
- For long-running sessions, prefer broader initial scopes over repeated escalations — approval fatigue erodes the security benefit of narrow scopes.

## Code Search Tools

Semantic search over indexed OpenFilter repositories. These tools are only registered when the server is started with the `code-search` dependency group and pre-built indexes.

| Tool | Description |
|------|-------------|
| `search` | Natural language search for code matching a description |
| `search_code` | Find code similar to a provided snippet |
| `get_chunk` | Retrieve a specific code chunk by ID |
| `read_file` | Read file contents from the indexed monorepo |

## Utility Tools

| Tool | Description |
|------|-------------|
| `poll_until_change` | Long-poll an entity until its state changes (useful for deployments) |
