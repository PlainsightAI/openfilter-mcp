---
name: openfilter-mcp
description: Use when building or operating Plainsight OpenFilter vision pipelines through the openfilter MCP server - scoping a session token, discovering entity types, creating a pipeline and its version, binding media, starting an instance and polling it to running.
---

# OpenFilter MCP

The `openfilter` MCP server exposes the whole Plainsight API through seven generic entity
tools plus scoped-token elicitation. Entity types and schemas come from the live OpenAPI
spec, so discovery happens at runtime, not from this file.

## Connect

Run the server, then point the client at `http://localhost:3000/mcp`:

```bash
psctl auth login
docker run -d -p 3000:3000 \
  -v "$(psctl token path):/root/.config/plainsight/token" \
  plainsightai/openfilter-mcp:latest-slim
```

Use the `-slim` tag unless the machine has an NVIDIA GPU: the full image links CUDA and
crashes at startup without one. Ready-made client configs live in `docs/demo/`
(Claude Code, Cursor, Codex, Gemini). Name the server `openfilter` so tool names match
this skill.

## Rules that cost time when ignored

- Entity type names are lowercase with no separators: `filterpipeline`, `pipelineinstance`,
  `sourceconfig`. Run `list_entity_types` to confirm before guessing.
- `list_entities` filters through `filters` (query string), not `query_params`.
  `path_params` is only for path placeholders.
- `get_entity` takes `id`; `get_entity_type_info` takes `entity_names` (a list).
- Every API call needs a scoped token. Request it first, once, for the whole task.

## Scope the session first

1. `list_entity_types` to see the resources involved (no token needed).
2. `list_grantable_scopes` to get the exact scope strings your role can grant.
3. `request_scoped_token` with every scope the task needs, as `resource:action` pairs.
   If the response carries an `approval_url`, tell the user to open it, then call
   `await_token_approval` with the `request_id`.
4. Need more later: `request_scoped_token(add_scopes="filterpipeline:update")` — a delta,
   not the full set again.

Ask for one approval covering the whole task. Broad reads are cheap; keep writes targeted.

## Build and run a pipeline

The sequence below is the validated path (PLAT-1420: driven end to end from Claude, Gemini
and Codex against dev).

1. `create_entity("filterpipeline", data={...})` — a graph of public filters, e.g.
   `video-in -> huggingface-vision -> webvis`.
2. `create_entity("pipelineversion", data={...})` — the version that gets deployed.
3. `list_entities("media", filters={"project": "<project_id>"})` — pick the media and bind
   it to the `video-in` node.
4. `create_entity("pipelineinstance", data={"pipeline_version_id": "...", "media_bindings": [...]})`
   — created in `pending`.
5. `entity_action("pipelineinstance", action="start", id="<instance_id>")`, then
   `poll_until_change(endpoint="/pipeline-instances/<id>", field="status", target_values="running,failed")`.

Read the schema with `get_entity_type_info` before each create: field names come from the
live spec, not from this example.

## Finish

Stop and delete what you created for a test run, and `clear_scoped_token` when the task is
done.
