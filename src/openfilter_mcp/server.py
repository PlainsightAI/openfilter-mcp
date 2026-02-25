"""OpenFilter MCP Server.

Provides tools for interacting with the Plainsight API and code search:

1. **Entity-Based API Tools** (8 tools instead of 100+):
   - `list_entity_types`: Discover available entities with optional full-text search
   - `get_entity_type_info`: Get full operation metadata for specific entity types
   - `create_entity`, `get_entity`, `list_entities`, `update_entity`, `delete_entity`: CRUD operations
   - `entity_action`: Custom actions like start, stop, cancel

2. **Code Search Tools** (manually defined, optional):
   - Semantic code search on indexed repositories
   - Code-to-code similarity search
   - File reading from the indexed monorepo

Configuration:
    ENABLE_CODE_SEARCH: Set to "true" to enable code search tools (default: "true").
                        Set to "false" to run the server without code search capabilities.
    REQUIRE_AUTH: Set to "true" to abort startup when no valid auth token is found
                  (default: "false"). Slim Docker images set this to "true" since they
                  have no code-search fallback and would otherwise serve zero tools.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import httpx
from fastmcp import FastMCP
from fastmcp.server.context import Context
from fastmcp.server.elicitation import AcceptedElicitation
from mcp.shared.exceptions import McpError

from openfilter_mcp.auth import (
    PLAINSIGHT_API_URL,
    get_api_url,
    get_auth_token,
    get_effective_org_id,
    is_plainsight_employee,
    read_psctl_token,
    AuthenticationError,
    TokenRefreshTransport,
)
from openfilter_mcp.entity_tools import register_entity_tools, SESSION_TOKEN_KEY, SESSION_TOKEN_META_KEY, ALLOW_UNSCOPED_TOKEN
from openfilter_mcp.approval_server import register_approval_routes
from openfilter_mcp.redact import register_sensitive
from openfilter_mcp.preindex_repos import MONOREPO_CLONE_DIR

logger = logging.getLogger(__name__)

# Session state keys are imported from entity_tools for consistency

# code-context is an optional dependency (install with `uv sync --group code-search`)
try:
    from code_context.indexing import INDEXES_DIR
    from code_context.main import _get_chunk, _search_index

    HAS_CODE_CONTEXT = True
except ImportError:
    INDEXES_DIR = None
    HAS_CODE_CONTEXT = False


# =============================================================================
# OpenAPI Spec Loading
# =============================================================================


def sanitize_openapi_spec(spec: dict) -> dict:
    """Sanitize OpenAPI spec for MCP compatibility.

    Removes properties with invalid names (MCP requires ^[a-zA-Z0-9_-]{1,64}$).

    Args:
        spec: The raw OpenAPI specification.

    Returns:
        The sanitized OpenAPI specification.
    """
    import re

    valid_pattern = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

    def clean_schema(schema: dict) -> dict:
        if not isinstance(schema, dict):
            return schema

        result = {}
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                # Filter out invalid property names (e.g., $schema)
                result[key] = {
                    prop_name: clean_schema(prop_value)
                    for prop_name, prop_value in value.items()
                    if valid_pattern.match(prop_name)
                }
            elif key == "required" and isinstance(value, list):
                # Filter required list to only include valid property names
                result[key] = [r for r in value if valid_pattern.match(r)]
            elif isinstance(value, dict):
                result[key] = clean_schema(value)
            elif isinstance(value, list):
                result[key] = [
                    clean_schema(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                result[key] = value

        return result

    return clean_schema(spec)


def strip_schema_from_response(data: Any) -> Any:
    """Recursively strip $schema keys from response data.

    Args:
        data: The response data (dict, list, or primitive).

    Returns:
        The data with $schema keys removed.
    """
    if isinstance(data, dict):
        return {
            key: strip_schema_from_response(value)
            for key, value in data.items()
            if key != "$schema"
        }
    elif isinstance(data, list):
        return [strip_schema_from_response(item) for item in data]
    else:
        return data


def get_openapi_spec() -> dict:
    """Fetch the OpenAPI specification from Plainsight API.

    Returns:
        The parsed and sanitized OpenAPI specification as a dictionary.

    Raises:
        httpx.HTTPStatusError: If the API request fails.
    """
    import httpx

    response = httpx.get(
        f"{PLAINSIGHT_API_URL}/openapi.json",
        timeout=30.0,
    )
    response.raise_for_status()
    spec = response.json()

    # Sanitize the spec to remove invalid property names (e.g., $schema)
    return sanitize_openapi_spec(spec)


def get_entity_spec() -> dict | None:
    """Fetch the entity specification from Plainsight API.

    Returns None if the endpoint is unavailable (older API versions).
    Falls back gracefully on 404 (expected for older versions) but logs
    server errors (5xx) at error level since they may warrant investigation.
    """
    try:
        response = httpx.get(
            f"{PLAINSIGHT_API_URL}/entity-spec",
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code >= 500:
            logger.error(
                "entity-spec endpoint returned server error (falling back to OpenAPI parsing): %s", e
            )
        else:
            logger.warning(
                "entity-spec endpoint unavailable, falling back to OpenAPI parsing: %s", e
            )
        return None
    except httpx.HTTPError as e:
        logger.warning(
            "entity-spec endpoint unreachable, falling back to OpenAPI parsing: %s", e
        )
        return None


class SchemaStrippingTransport(httpx.AsyncBaseTransport):
    """Custom transport that strips $schema from JSON responses.

    This prevents MCP output validation errors when the API returns
    $schema fields that aren't declared in the OpenAPI response schemas.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport):
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)

        # Only process JSON responses
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            # Read and parse the response body
            body = await response.aread()
            try:
                data = json.loads(body)
                cleaned_data = strip_schema_from_response(data)
                cleaned_body = json.dumps(cleaned_data).encode("utf-8")

                # Create a new response with the cleaned body
                return httpx.Response(
                    status_code=response.status_code,
                    headers=response.headers,
                    content=cleaned_body,
                    request=request,
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                # If we can't parse as JSON, return original response
                return httpx.Response(
                    status_code=response.status_code,
                    headers=response.headers,
                    content=body,
                    request=request,
                )

        return response


def create_authenticated_client(timeout: float = 30.0):
    """Create an authenticated async HTTP client for Plainsight API.

    Supports cross-tenant operations for Plainsight employees via PS_TARGET_ORG_ID.

    Args:
        timeout: Request timeout in seconds.

    Returns:
        Configured httpx.AsyncClient instance with schema-stripping middleware
        and automatic 401 retry via token refresh.

    Raises:
        AuthenticationError: If no valid token is available.
    """
    token = get_auth_token()
    if not token:
        raise AuthenticationError("No authentication token available")

    headers = {"Authorization": f"Bearer {token}"}

    # Use effective org ID (supports cross-tenant for Plainsight employees)
    org_id = get_effective_org_id(token)
    if org_id:
        headers["X-Scope-OrgID"] = org_id

    # Create transport chain: base -> token refresh -> schema stripping
    # Token refresh handles 401s by refreshing the token and retrying
    # Schema stripping removes $schema keys from JSON responses
    base_transport = httpx.AsyncHTTPTransport()
    token_refresh_transport = TokenRefreshTransport(
        transport=base_transport,
        get_org_id=get_effective_org_id,
    )
    schema_stripping_transport = SchemaStrippingTransport(token_refresh_transport)

    return httpx.AsyncClient(
        base_url=get_api_url(),
        headers=headers,
        timeout=timeout,
        transport=schema_stripping_transport,
    )


# =============================================================================
# Code Search Utilities
# =============================================================================


def get_latest_index_name() -> str:
    """Finds the most recently modified index name related to MONOREPO_CLONE_DIR."""
    try:
        latest_index_entry = max(
            (
                entry
                for entry in os.scandir(INDEXES_DIR)
                if entry.is_dir() and entry.name.startswith(MONOREPO_CLONE_DIR)
            ),
            key=lambda entry: entry.stat().st_mtime,
        )
        return latest_index_entry.name
    except ValueError:
        raise FileNotFoundError(
            f"No index found for {MONOREPO_CLONE_DIR} in {INDEXES_DIR}"
        )


def _is_subpath(path, parent_directory):
    path = os.path.realpath(path)
    parent_directory = os.path.realpath(parent_directory)
    return path.startswith(parent_directory + os.sep)


def _real_path(path):
    """Check that a path resolution is secure and valid."""
    path = os.path.join(MONOREPO_CLONE_DIR, path)
    if _is_subpath(path, MONOREPO_CLONE_DIR):
        return path
    else:
        raise FileNotFoundError("Path is not within the monorepo directory.")


# =============================================================================
# Create MCP Server from OpenAPI Spec
# =============================================================================

_SERVER_INSTRUCTIONS = """
OpenFilter MCP provides entity CRUD tools for the Plainsight API and semantic code search over the Plainsight monorepo.

**Token scoping (important):** Before making ANY API calls, you should request a scoped token with the minimum permissions your task requires. This follows the principle of least privilege and protects against accidental modifications to resources outside the task scope — even for read-only operations.

How to scope a session:
1. Determine which entity types and actions your task needs (use `list_entity_types` to discover available resources).
2. Call `request_scoped_token` with a comma-separated list of "resource:action" scopes.
   - The user will be asked to approve via an interactive dialog or browser page.
   - If the response includes an `approval_url`, tell the user to open it in their browser,
     then call `await_token_approval` with the `request_id` to block until they respond.
3. Once approved, the scoped token is stored in the session and used automatically for all subsequent API calls.
4. Use `get_token_status` to check current permissions and expiry.
5. Use `clear_scoped_token` to revert to the default token (e.g., to request different scopes).

Scope format: "resource:action" where resource is any entity type and action is read, create, update, delete, or * for all actions. Examples: "project:read,filterpipeline:read", "filterpipeline:*,pipelineinstance:*".

IMPORTANT: Resource names and entity_type values are lowercase with NO underscores or hyphens. Use 'filterpipeline' (not 'filter_pipeline'), 'pipelineinstance' (not 'pipeline_instance'), 'sourceconfig' (not 'source_config'). Use list_entity_types() to discover valid names.

When planning scopes, think ahead about the FULL task. If the user asks to "see what's running in my project," you'll need project + filterpipeline + pipelineinstance + filter access — request all of these upfront in a single request_scoped_token call rather than escalating incrementally. This reduces approval fatigue. Only escalate if a genuinely unexpected need arises (e.g., the user asks you to modify something after a read-only investigation).

Always request the narrowest scopes possible. Prefer read-only scopes unless writes are explicitly needed. Do not request wildcard (*) scopes unless the task genuinely requires all actions on a resource.

Tool usage tips:
- list_entities uses `filters` (not `query_params`) for HTTP query parameters. Example: list_entities('filterpipeline', filters={'project': '<id>'}).
- Most list endpoints filter by project via query params, not path params. Check get_entity_type_info() to see which params go where.
- get_entity uses `id` (not `entity_id`) as the parameter name.

Note: By default, all API operations require a scoped token. You MUST call request_scoped_token before any entity operations. This requirement can be relaxed by setting OPF_MCP_ALLOW_UNSCOPED_TOKEN=true, but this is not recommended.
""".strip()


def create_mcp_server() -> FastMCP:
    """Create the MCP server with entity-based API tools and code search tools.

    Returns:
        A FastMCP server instance with entity CRUD tools plus code search tools.
        If no authentication token is available, only code search tools are registered.

    Raises:
        SystemExit: If REQUIRE_AUTH is set and no valid token is found.
    """
    # Create base MCP server
    mcp = FastMCP(name="OpenFilter MCP", instructions=_SERVER_INSTRUCTIONS)

    # Register approval routes on the MCP server (reuses port 3000, works in Docker)
    approval_registry = register_approval_routes(mcp)

    # When REQUIRE_AUTH is set (e.g. slim Docker images that have no code-search
    # fallback), refuse to start if we cannot authenticate -- an unauthenticated
    # slim server would register zero tools and silently do nothing.
    require_auth = os.getenv("REQUIRE_AUTH", "false").lower() == "true"

    # Try to create authenticated client and load OpenAPI tools
    # If no token is available, we'll still create a server with code search tools
    token = get_auth_token()
    client = None
    openapi_spec = None
    has_auth = False

    if token:
        try:
            client = create_authenticated_client()
            openapi_spec = get_openapi_spec()
            has_auth = True
        except AuthenticationError as exc:
            if require_auth:
                raise SystemExit(
                    "REQUIRE_AUTH is set but authentication failed: "
                    f"{exc}\n"
                    "Provide a valid token via OPENFILTER_TOKEN env var "
                    "or mount a psctl token file."
                ) from exc
            # Token was available but invalid - proceed without API tools

    if require_auth and not has_auth:
        raise SystemExit(
            "REQUIRE_AUTH is set but no authentication token was found.\n"
            "Provide a valid token via OPENFILTER_TOKEN env var "
            "or mount a psctl token file (~/.config/plainsight/token)."
        )

    # Register entity-based CRUD tools if authenticated
    registry = None
    entity_handler = None
    if has_auth and openapi_spec and client:
        entity_spec = get_entity_spec()
        registry, entity_handler = register_entity_tools(mcp, client, openapi_spec, entity_spec=entity_spec, approval_registry=approval_registry)

    # =========================================================================
    # Code Search Tools (manually defined - not part of Plainsight API)
    # =========================================================================

    # Check if code search is enabled (default: true)
    # Requires the code-search extra: `uv sync --group code-search`
    enable_code_search = os.getenv("ENABLE_CODE_SEARCH", "true").lower() == "true"

    if enable_code_search and HAS_CODE_CONTEXT:
        # Get the latest index name for code search tools
        latest_index_name = get_latest_index_name()

        @mcp.tool()
        def search(query: str, top_k: int = 10) -> Dict[str, Any]:
            """Searches a semantic index for code matching a natural language description.

            Returns the top_k most relevant chunks with their scores and metadata."""
            return _search_index(latest_index_name, query, "nl2code", top_k)

        @mcp.tool()
        def search_code(code_query: str, top_k: int = 10) -> Dict[str, Any]:
            """Searches a semantic index for code similar to the provided code snippet.

            Returns the top_k most relevant chunks with their scores and metadata."""
            return _search_index(latest_index_name, code_query, "code2code", top_k)

        @mcp.tool()
        def get_chunk(chunk_id: int) -> Dict[str, Any]:
            """Retrieves the content and metadata of a specific chunk by its ID.

            Returns: JSON object with filepath, startLine, endLine, and content."""
            return _get_chunk(latest_index_name, chunk_id)

        @mcp.tool()
        def read_file(filepath: str, start_line: int = 0, line_count: int = 100) -> str:
            """Reads the content of a virtual file in the monorepo index.

            Returns: The content of the file as a string."""
            with open(_real_path(filepath), "r") as file:
                content = file.read()
            lines = content.splitlines()
            content = "\n".join(lines[start_line : start_line + line_count])
            return content

    elif enable_code_search and not HAS_CODE_CONTEXT:
        import sys
        print(
            "WARNING: ENABLE_CODE_SEARCH is true but code-context is not installed. "
            "Install with: uv sync --group code-search",
            file=sys.stderr,
        )

    # =========================================================================
    # Generic Polling Tool (only available with authentication)
    # =========================================================================

    if has_auth and client:

        def _get_cross_tenant_headers(org_id: str | None) -> Dict[str, str] | None:
            """Get headers for cross-tenant access if allowed."""
            if not org_id:
                return None
            token = get_auth_token()
            if token and is_plainsight_employee(token):
                return {"X-Scope-OrgID": org_id}
            return None

        @mcp.tool()
        async def poll_until_change(
            endpoint: str,
            field: str,
            target_values: str,
            poll_interval_seconds: int = 5,
            timeout_seconds: int = 600,
            org_id: str | None = None,
            ctx: Context | None = None,
        ) -> Dict[str, Any]:
            """Poll an API endpoint until a field reaches one of the target values.

            This is a generic polling tool that can wait for any async operation to complete.

            Args:
                endpoint: The API endpoint to poll (e.g., "/trainings/abc-123").
                field: The field name to check in the response (e.g., "status").
                target_values: Comma-separated list of values that indicate completion (e.g., "completed,failed,cancelled").
                poll_interval_seconds: How often to check (default: 5 seconds).
                timeout_seconds: Maximum time to wait (default: 600 seconds = 10 minutes).
                org_id: Optional organization ID for cross-tenant access (Plainsight employees only).

            Returns:
                Object with status ("completed" or "timeout"), the final result, and elapsed time.

            Examples:
                - Wait for training: endpoint="/trainings/{id}", field="status", target_values="completed,failed,cancelled"
                - Wait for synthetic video: endpoint="/projects/{project_id}/synthetic-videos/{job_id}", field="status", target_values="completed,failed"
                - Wait for recording: endpoint="/projects/{project_id}/recordings/{id}", field="status", target_values="completed,failed,cancelled"
                - Wait for pipeline: endpoint="/pipeline-instances/{id}", field="status", target_values="running,failed,error"
            """
            targets = [v.strip() for v in target_values.split(",")]
            elapsed = 0
            last_value = None

            # Build headers using the same logic as entity CRUD tools:
            # scoped token with expiry detection, cross-tenant employee check.
            if entity_handler:
                headers = await entity_handler._get_request_headers(org_id, ctx)
            else:
                headers = None
            if headers is None:
                headers = {}

            while elapsed < timeout_seconds:
                response = await client.get(endpoint, headers=headers if headers else None)
                response.raise_for_status()
                data = response.json()

                last_value = data.get(field)
                if last_value in targets:
                    return {
                        "status": "completed",
                        "field": field,
                        "final_value": last_value,
                        "result": data,
                        "elapsed_seconds": elapsed,
                    }

                await asyncio.sleep(poll_interval_seconds)
                elapsed += poll_interval_seconds

            return {
                "status": "timeout",
                "message": f"Field '{field}' did not reach target values {targets} within {timeout_seconds} seconds",
                "field": field,
                "last_value": last_value,
                "elapsed_seconds": elapsed,
            }

    # =========================================================================
    # Token Scoping Tools (available when authenticated)
    # =========================================================================

    # Session-scoped prefix so each server instance gets unique token names,
    # avoiding 409 conflicts with tokens from other sessions.
    import secrets as _secrets
    _session_id = _secrets.token_hex(4)  # e.g. "a1b2c3d4"

    async def _create_and_activate_token(
        ctx: Context,
        http_client: httpx.AsyncClient,
        name: str,
        scope_list: list[str],
        expires_at: datetime,
        effective_org_id: str,
    ) -> Dict[str, Any]:
        """Create a scoped API token and activate it in session state.

        Shared by request_scoped_token and await_token_approval to avoid
        duplicating the POST /api-tokens → register → set_state → ctx.info flow.

        Returns:
            Success dict with status "active", or error dict on failure.
        """
        org_headers = {"X-Scope-OrgID": effective_org_id}
        payload = {
            "name": name,
            "scopes": scope_list,
            "expires_at": expires_at.isoformat(),
        }
        response = await http_client.post("/api-tokens", json=payload, headers=org_headers)

        # Handle 409 Conflict: a token with this name already exists (e.g.
        # leftover from a crashed session). Retry once with a disambiguated name.
        if response.status_code == 409:
            name = f"{name}-{_secrets.token_hex(2)}"
            payload["name"] = name
            logger.info(f"Token name conflict, retrying as '{name}'")
            response = await http_client.post("/api-tokens", json=payload, headers=org_headers)

        if response.status_code >= 400:
            try:
                error_body = response.json()
            except Exception:
                error_body = response.text
            return {
                "error": f"Failed to create scoped token: API returned {response.status_code}",
                "details": error_body,
            }

        result = response.json()
        plaintext_token = result.get("token")
        token_id = result.get("id")

        if not plaintext_token:
            return {"error": "API did not return a token in the response."}

        stub = register_sensitive(plaintext_token, label="scoped-token")

        # Store the token in session state — the LLM never sees this
        await ctx.set_state(SESSION_TOKEN_KEY, plaintext_token)
        await ctx.set_state(SESSION_TOKEN_META_KEY, {
            "id": token_id,
            "name": result.get("name", name),
            "scopes": scope_list,
            "expires_at": expires_at.isoformat(),
            "org_id": effective_org_id,
        })

        # Log the token to the user's UI (invisible to the LLM)
        await ctx.info(
            f"Scoped token activated (ID: {token_id}). "
            f"Log reference: {stub} — use this ID when reporting tool errors. "
            f"This token will be used automatically for subsequent API calls."
        )

        logger.info(f"Scoped token created: id={token_id}, scopes={scope_list}")

        return {
            "status": "active",
            "message": "Scoped token created and activated for this session.",
            "token_name": result.get("name", name),
            "scopes": scope_list,
            "expires_at": expires_at.isoformat(),
        }

    # Pending web-based approvals: request_id -> {session, scope_list, name, ...}
    _pending_approvals: Dict[str, Any] = {}

    if has_auth and client:

        @mcp.tool()
        async def request_scoped_token(
            scopes: str,
            name: str = f"mcp-{_session_id}",
            expires_in_hours: int = 1,
            org_id: str | None = None,
            ctx: Context | None = None,
        ) -> Dict[str, Any]:
            """Request a scoped API token with limited permissions for this session.

            This creates a new API token with only the permissions you need,
            replacing the default full-access token for subsequent API calls.
            The user will be asked to approve the requested scopes via an
            interactive dialog (elicitation). If your MCP client does not support
            elicitation, a browser-based approval page is started instead and
            the tool returns immediately with an approval_url and request_id.
            You MUST tell the user to open the URL, then call
            await_token_approval with the request_id to block until they respond.

            You will NOT see the token value — it is stored securely in the
            session and used automatically for subsequent API calls.

            Args:
                scopes: Comma-separated list of permission scopes to request.
                    Format: "resource:action" where action is read, create, update, delete, or *.
                    Use "resource:*" for all actions on a resource.
                    IMPORTANT: Resource names are lowercase with NO underscores or
                    hyphens — e.g., 'filterpipeline' not 'filter_pipeline',
                    'pipelineinstance' not 'pipeline_instance'. Use
                    list_entity_types() to discover valid resource names.
                    Examples: "project:read,filterpipeline:read,pipelineinstance:read",
                             "filterpipeline:*,pipelineinstance:*"
                name: A name for this token (for identification in the portal).
                expires_in_hours: How long the token should be valid (default: 1 hour).
                org_id: Optional organization ID to create the token for. If not provided,
                    the org ID is derived from the current psctl/auth token.

            Returns:
                Confirmation of which scopes were granted (token value is hidden).
            """
            if not ctx:
                return {"error": "This tool requires a session context. Ensure your MCP client provides one."}

            scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
            if not scope_list:
                return {"error": "No scopes provided. Specify at least one scope like 'project:read'."}

            valid_actions = {"read", "create", "update", "delete", "*"}
            valid_resources = set(registry.list_entities()) if registry else set()
            errors = []
            for s in scope_list:
                parts = s.split(":", 1)
                if len(parts) != 2:
                    errors.append(f"{s} (expected 'resource:action')")
                    continue
                resource, action = parts
                if action not in valid_actions:
                    errors.append(f"{s} (unknown action '{action}', expected: {', '.join(sorted(valid_actions))})")
                elif valid_resources and resource not in valid_resources:
                    suggestions = registry.suggest_entity(resource, limit=3) if registry else []
                    if suggestions:
                        hint = f"did you mean: {', '.join(suggestions)}?"
                        errors.append(f"{s} (unknown resource '{resource}', {hint})")
                    else:
                        errors.append(f"{s} (unknown resource '{resource}', available: {', '.join(sorted(valid_resources))})")
            if errors:
                return {"error": f"Invalid scopes: {errors}"}

            if expires_in_hours < 1:
                return {"error": "expires_in_hours must be at least 1."}
            if expires_in_hours > 720:  # 30 days
                return {"error": "expires_in_hours cannot exceed 720 (30 days)."}

            expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)

            # Ask the user for approval
            scope_lines = "\n".join(f"  - {s}" for s in scope_list)
            approved = False
            try:
                approval = await ctx.elicit(
                    f"The AI agent is requesting a scoped API token.\n\n"
                    f"Token name: {name}\n"
                    f"Requested scopes:\n{scope_lines}\n\n"
                    f"Expires: {expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                    f"Do you approve?",
                    ["Approve", "Deny"],
                )
                approved = isinstance(approval, AcceptedElicitation) and approval.data == "Approve"
            except McpError as e:
                err_msg = str(e)
                if "Method not found" not in err_msg and "not supported" not in err_msg:
                    raise

                # Compute org_id now so it's available when the approval completes
                effective_org_id = org_id
                if not effective_org_id:
                    token = read_psctl_token() or get_auth_token()
                    effective_org_id = get_effective_org_id(token)
                if not effective_org_id:
                    return {"error": "Cannot determine organization ID from current token."}

                # Client doesn't support elicitation — fall back to browser approval
                session = approval_registry.create_session(
                    title="Scoped Token Request",
                    message="The AI agent is requesting a scoped API token with the following permissions.",
                    details={
                        "Token name": name,
                        "Scopes": scope_list,
                        "Expires": expires_at.strftime("%Y-%m-%d %H:%M UTC"),
                    },
                    base_url=f"http://localhost:{os.getenv('PORT', '3000')}",
                )

                # Store pending state so await_token_approval can finalize
                import secrets
                request_id = secrets.token_urlsafe(8)
                _pending_approvals[request_id] = {
                    "session": session,
                    "scope_list": scope_list,
                    "name": name,
                    "expires_at": expires_at,
                    "effective_org_id": effective_org_id,
                }

                # Clean up stale entry if the user never calls await_token_approval
                session.add_timeout_callback(lambda rid=request_id: _pending_approvals.pop(rid, None))

                # Return immediately — the agent must tell the user about the
                # URL, then call await_token_approval to block on the result.
                return {
                    "status": "pending_approval",
                    "approval_url": session.url,
                    "request_id": request_id,
                    "message": "Your MCP client does not support interactive approval. "
                    "Tell the user to open the approval URL in their browser, then call "
                    "await_token_approval with the request_id.",
                }

            if not approved:
                return {"status": "denied", "message": "User denied the token request."}

            # Get the effective org ID
            effective_org_id = org_id
            if not effective_org_id:
                token = read_psctl_token() or get_auth_token()
                effective_org_id = get_effective_org_id(token)
            if not effective_org_id:
                return {"error": "Cannot determine organization ID from current token."}

            # Create the scoped token via the Plainsight API
            try:
                return await _create_and_activate_token(
                    ctx, client, name, scope_list, expires_at, effective_org_id,
                )
            except Exception as e:
                logger.error(f"Failed to create scoped token: {e}")
                return {"error": f"Failed to create scoped token: {str(e)}"}

        @mcp.tool()
        async def await_token_approval(
            request_id: str,
            ctx: Context | None = None,
        ) -> Dict[str, Any]:
            """Wait for the user to approve or deny a pending token request.

            Call this after request_scoped_token returns status "pending_approval".
            This tool BLOCKS until the user clicks Approve or Deny in the browser
            page, or the approval times out. Once approved, the scoped token is
            created and activated automatically.

            Args:
                request_id: The request_id returned by request_scoped_token.

            Returns:
                Token status: "active" (approved and created), "denied", "timeout", or "error".
            """
            if not ctx:
                return {"error": "This tool requires a session context."}

            pending = _pending_approvals.get(request_id)
            if not pending:
                return {"error": f"No pending approval found for request_id '{request_id}'. "
                        "It may have already been completed or expired."}

            session = pending["session"]

            # Block until the user responds or timeout
            result = await session.wait()

            # Clean up pending state (may already be removed by timeout callback)
            _pending_approvals.pop(request_id, None)

            if result != "approve":
                return {"status": "denied", "message": "User denied the token request (or it timed out)."}

            # User approved — create the scoped token
            scope_list = pending["scope_list"]
            name = pending["name"]
            expires_at = pending["expires_at"]
            effective_org_id = pending["effective_org_id"]

            try:
                return await _create_and_activate_token(
                    ctx, client, name, scope_list, expires_at, effective_org_id,
                )
            except Exception as e:
                logger.error(f"Failed to create scoped token: {e}")
                return {"error": f"Failed to create scoped token: {str(e)}"}

        @mcp.tool()
        async def get_token_status(ctx: Context | None = None) -> Dict[str, Any]:
            """Check the current token status for this session.

            Returns information about whether a scoped token is active,
            what permissions it has, and when it expires.
            The actual token value is never shown.

            Returns:
                Token status including scopes and expiration, or indication
                that the default (full-access) token is being used.
            """
            if not ctx:
                return {"status": "unknown", "message": "No session context available."}
            meta = await ctx.get_state(SESSION_TOKEN_META_KEY)
            if meta:
                expires_at_str = meta.get("expires_at")
                if expires_at_str:
                    expires_at = datetime.fromisoformat(expires_at_str)
                    if expires_at < datetime.now(timezone.utc):
                        return {
                            "status": "expired",
                            "message": f"Scoped token '{meta.get('name')}' has expired. Use clear_scoped_token and request_scoped_token to get a new one.",
                            "expired_at": expires_at_str,
                        }
                return {
                    "status": "scoped",
                    "message": "A scoped token is active for this session.",
                    "token_name": meta.get("name"),
                    "scopes": meta.get("scopes"),
                    "expires_at": meta.get("expires_at"),
                }
            return {
                "status": "default",
                "message": "Using the default server token (full access). "
                "Use request_scoped_token to create a limited-permission token.",
            }

        @mcp.tool()
        async def clear_scoped_token(ctx: Context | None = None) -> Dict[str, Any]:
            """Clear the scoped token for this session, reverting to the default server token.

            Use this if the scoped token has expired or you need different permissions.

            Returns:
                Confirmation that the scoped token was cleared.
            """
            if not ctx:
                return {"error": "This tool requires an interactive session."}
            meta = await ctx.get_state(SESSION_TOKEN_META_KEY)
            await ctx.set_state(SESSION_TOKEN_KEY, None)
            await ctx.set_state(SESSION_TOKEN_META_KEY, None)
            if meta:
                return {"status": "cleared", "message": f"Scoped token '{meta.get('name')}' cleared. Using default server token."}
            return {"status": "no_change", "message": "No scoped token was active."}

    return mcp


# =============================================================================
# Main Entry Point
# =============================================================================


def main():
    # Ensure necessary directories exist
    if HAS_CODE_CONTEXT:
        os.makedirs(INDEXES_DIR, exist_ok=True)

    # Create server at runtime (not at import time)
    mcp = create_mcp_server()
    port = int(os.getenv("PORT", "3000"))
    mcp.run(transport="http", port=port, host="0.0.0.0")


if __name__ == "__main__":
    main()
