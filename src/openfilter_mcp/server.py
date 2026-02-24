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
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import httpx

# code-context is an optional dependency (install with `uv sync --extra code-search`)
try:
    from code_context.indexing import INDEXES_DIR
    from code_context.main import _get_chunk, _search_index

    HAS_CODE_CONTEXT = True
except ImportError:
    INDEXES_DIR = "indexes"
    HAS_CODE_CONTEXT = False

from fastmcp import FastMCP
from fastmcp.server.context import Context
from fastmcp.server.elicitation import AcceptedElicitation

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
from openfilter_mcp.entity_tools import register_entity_tools
from openfilter_mcp.preindex_repos import MONOREPO_CLONE_DIR

logger = logging.getLogger(__name__)

# Session state key for the scoped API token
_SESSION_TOKEN_KEY = "scoped_api_token"
_SESSION_TOKEN_META_KEY = "scoped_api_token_meta"


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

Token scoping: The server starts with a default token that has full API access. Before performing write or delete operations, request a scoped token with the minimum permissions needed:
- Use `request_scoped_token` with a comma-separated list of "resource:action" scopes. The user will be asked to approve.
- Once approved, the scoped token is stored in the session and used automatically for all subsequent API calls.
- Use `get_token_status` to check current permissions and expiry.
- Use `clear_scoped_token` to revert to the default full-access token.

Scope format: "resource:action" where action is read, create, update, delete, or * for all actions on that resource. Examples: "project:read,deployment:create", "filterpipeline:*,pipelineinstance:*".

Always prefer narrowly scoped tokens for operations that modify or delete data.
""".strip()


def create_mcp_server() -> FastMCP:
    """Create the MCP server with entity-based API tools and code search tools.

    Returns:
        A FastMCP server instance with entity CRUD tools plus code search tools.
        If no authentication token is available, only code search tools are registered.
    """
    # Create base MCP server
    mcp = FastMCP(name="OpenFilter MCP", instructions=_SERVER_INSTRUCTIONS)

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
        except AuthenticationError:
            # Token was available but invalid - proceed without API tools
            pass

    # Register entity-based CRUD tools if authenticated
    if has_auth and openapi_spec and client:
        register_entity_tools(mcp, client, openapi_spec)

    # =========================================================================
    # Code Search Tools (manually defined - not part of Plainsight API)
    # =========================================================================

    # Check if code search is enabled (default: true)
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
            "Install with: uv sync --extra code-search",
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
            headers = _get_cross_tenant_headers(org_id)

            while elapsed < timeout_seconds:
                response = await client.get(endpoint, headers=headers)
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

    if has_auth and client:

        @mcp.tool()
        async def request_scoped_token(
            scopes: str,
            name: str = "openfilter-mcp-agent",
            expires_in_hours: int = 1,
            ctx: Context = None,
        ) -> Dict[str, Any]:
            """Request a scoped API token with limited permissions for this session.

            This creates a new API token with only the permissions you need,
            replacing the default full-access token for subsequent API calls.
            The user will be asked to approve the requested scopes.

            You will NOT see the token value — it is stored securely in the
            session and used automatically for subsequent API calls.

            Args:
                scopes: Comma-separated list of permission scopes to request.
                    Format: "resource:action" where action is read, create, update, or delete.
                    Use "resource:*" for all actions on a resource.
                    Available resources: project, organization, deployment, model, training,
                    filter, filterpipeline, pipelineversion, pipelineinstance, sourceconfig,
                    filterimage, filterregistry, filtersubscription, filtertopic,
                    filterparameter, secret, syntheticvideo, videoupload, videorecording,
                    videocorpus, artifact, agent, apitoken, user.
                    Examples: "project:read,deployment:read,deployment:create",
                             "filterpipeline:*,pipelineinstance:*"
                name: A name for this token (for identification in the portal).
                expires_in_hours: How long the token should be valid (default: 1 hour).

            Returns:
                Confirmation of which scopes were granted (token value is hidden).
            """
            if not ctx:
                return {"error": "This tool requires an interactive session with elicitation support."}

            scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
            if not scope_list:
                return {"error": "No scopes provided. Specify at least one scope like 'project:read'."}

            scope_pattern = re.compile(r'^[a-z]+:(read|create|update|delete|\*)$')
            invalid = [s for s in scope_list if not scope_pattern.match(s)]
            if invalid:
                return {"error": f"Invalid scope format: {invalid}. Expected 'resource:action' where action is read, create, update, delete, or *."}

            if expires_in_hours < 1:
                return {"error": "expires_in_hours must be at least 1."}
            if expires_in_hours > 720:  # 30 days
                return {"error": "expires_in_hours cannot exceed 720 (30 days)."}

            expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)

            # Ask the user for approval via elicitation (the LLM never sees this dialog)
            scope_lines = "\n".join(f"  - {s}" for s in scope_list)
            approval = await ctx.elicit(
                f"The AI agent is requesting a scoped API token.\n\n"
                f"Token name: {name}\n"
                f"Requested scopes:\n{scope_lines}\n\n"
                f"Expires: {expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"Do you approve?",
                ["Approve", "Deny"],
            )

            # Check if user approved
            if not isinstance(approval, AcceptedElicitation) or approval.data != "Approve":
                return {"status": "denied", "message": "User denied the token request."}

            # Get the user's org ID from the current token
            token = read_psctl_token() or get_auth_token()
            org_id = get_effective_org_id(token)
            if not org_id:
                return {"error": "Cannot determine organization ID from current token."}

            # Create the scoped token via the Plainsight API
            try:
                response = await client.post(
                    "/api-tokens",
                    json={
                        "name": name,
                        "scopes": scope_list,
                        "expires_at": expires_at.isoformat(),
                    },
                    headers={"X-Scope-OrgID": org_id},
                )

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

                # Store the token in session state — the LLM never sees this
                await ctx.set_state(_SESSION_TOKEN_KEY, plaintext_token)
                await ctx.set_state(_SESSION_TOKEN_META_KEY, {
                    "id": token_id,
                    "name": result.get("name", name),
                    "scopes": scope_list,
                    "expires_at": expires_at.isoformat(),
                    "org_id": org_id,
                })

                # Log the token to the user's UI (invisible to the LLM)
                await ctx.info(
                    f"Scoped token activated (ID: {token_id}). "
                    f"This token will be used automatically for subsequent API calls. "
                    f"You can revoke it in the portal or via the API."
                )

                logger.info(f"Scoped token created: id={token_id}, scopes={scope_list}")

                # Return only metadata to the LLM — no token value
                return {
                    "status": "active",
                    "message": "Scoped token created and activated for this session.",
                    "token_name": result.get("name", name),
                    "scopes": scope_list,
                    "expires_at": expires_at.isoformat(),
                }

            except Exception as e:
                logger.error(f"Failed to create scoped token: {e}")
                return {"error": f"Failed to create scoped token: {str(e)}"}

        @mcp.tool()
        async def get_token_status(ctx: Context = None) -> Dict[str, Any]:
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
            meta = await ctx.get_state(_SESSION_TOKEN_META_KEY)
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
        async def clear_scoped_token(ctx: Context = None) -> Dict[str, Any]:
            """Clear the scoped token for this session, reverting to the default server token.

            Use this if the scoped token has expired or you need different permissions.

            Returns:
                Confirmation that the scoped token was cleared.
            """
            if not ctx:
                return {"error": "This tool requires an interactive session."}
            meta = await ctx.get_state(_SESSION_TOKEN_META_KEY)
            await ctx.set_state(_SESSION_TOKEN_KEY, None)
            await ctx.set_state(_SESSION_TOKEN_META_KEY, None)
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
    mcp.run(transport="http", port=3000, host="0.0.0.0")


if __name__ == "__main__":
    main()
