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
