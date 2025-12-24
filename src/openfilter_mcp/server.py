"""OpenFilter MCP Server.

Provides tools for interacting with the Plainsight API and code search:

1. **Plainsight API Tools** (auto-generated from OpenAPI):
   - All Plainsight API endpoints are automatically exposed as MCP tools
   - Video corpus management, test management, synthetic video generation, etc.

2. **Code Search Tools** (manually defined):
   - Semantic code search on indexed repositories
   - Code-to-code similarity search
   - File reading from the indexed monorepo
"""

import asyncio
import json
import os
from typing import Any, Dict

import httpx

from code_context.indexing import INDEXES_DIR
from code_context.main import _get_chunk, _search_index
from fastmcp import FastMCP
from fastmcp.server.openapi import RouteMap, MCPType
from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransform

from openfilter_mcp.auth import (
    PLAINSIGHT_API_URL,
    get_auth_token,
    get_org_id_from_token,
    AuthenticationError,
)
from openfilter_mcp.preindex_repos import MONOREPO_CLONE_DIR


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

    Args:
        timeout: Request timeout in seconds.

    Returns:
        Configured httpx.AsyncClient instance with schema-stripping middleware.

    Raises:
        AuthenticationError: If no valid token is available.
    """
    token = get_auth_token()
    if not token:
        raise AuthenticationError("No authentication token available")

    headers = {"Authorization": f"Bearer {token}"}

    org_id = get_org_id_from_token(token)
    if org_id:
        headers["X-Scope-OrgID"] = org_id

    # Create base transport and wrap with schema-stripping middleware
    base_transport = httpx.AsyncHTTPTransport()
    schema_stripping_transport = SchemaStrippingTransport(base_transport)

    return httpx.AsyncClient(
        base_url=PLAINSIGHT_API_URL,
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


def create_mcp_server() -> FastMCP:
    """Create the MCP server with OpenAPI-generated tools and code search tools.

    Returns:
        A FastMCP server instance with all Plainsight API tools plus code search tools.
    """
    openapi_spec = get_openapi_spec()
    client = create_authenticated_client()

    # Get org_id from token to use as default for X-Scope-OrgID parameter
    token = get_auth_token()
    org_id = get_org_id_from_token(token) if token else None

    def hide_org_id_param(route, component):
        """Hide the X-Scope-OrgID parameter from tools since it's auto-injected from the token."""
        if isinstance(component, Tool) and org_id:
            # Check if this tool has X-Scope-OrgID parameter
            if component.parameters and "X-Scope-OrgID" in component.parameters.get("properties", {}):
                # Transform the tool to hide the X-Scope-OrgID parameter
                transformed = Tool.from_tool(
                    component,
                    transform_args={
                        "X-Scope-OrgID": ArgTransform(hide=True, default=org_id)
                    },
                )
                # Copy transformed attributes back to component
                component.parameters = transformed.parameters
                component.fn = transformed.fn

    # Define route mappings - all endpoints become tools
    route_maps = [
        # Exclude any internal endpoints
        RouteMap(
            pattern=r"^/internal/.*",
            mcp_type=MCPType.EXCLUDE,
        ),
        # All other endpoints become tools
        RouteMap(
            methods=["*"],
            pattern=r".*",
            mcp_type=MCPType.TOOL,
        ),
    ]

    mcp = FastMCP.from_openapi(
        openapi_spec=openapi_spec,
        client=client,
        name="OpenFilter MCP",
        route_maps=route_maps,
        timeout=30.0,
        mcp_component_fn=hide_org_id_param,
    )

    # Get the latest index name for code search tools
    latest_index_name = get_latest_index_name()

    # =========================================================================
    # Code Search Tools (manually defined - not part of Plainsight API)
    # =========================================================================

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

    # =========================================================================
    # Generic Polling Tool
    # =========================================================================

    @mcp.tool()
    async def poll_until_change(
        endpoint: str,
        field: str,
        target_values: str,
        poll_interval_seconds: int = 5,
        timeout_seconds: int = 600,
    ) -> Dict[str, Any]:
        """Poll an API endpoint until a field reaches one of the target values.

        This is a generic polling tool that can wait for any async operation to complete.

        Args:
            endpoint: The API endpoint to poll (e.g., "/trainings/abc-123").
            field: The field name to check in the response (e.g., "status").
            target_values: Comma-separated list of values that indicate completion (e.g., "completed,failed,cancelled").
            poll_interval_seconds: How often to check (default: 5 seconds).
            timeout_seconds: Maximum time to wait (default: 600 seconds = 10 minutes).

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

        while elapsed < timeout_seconds:
            response = await client.get(endpoint)
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

    return mcp


# =============================================================================
# Main Entry Point
# =============================================================================


def main():
    # Ensure necessary directories exist
    os.makedirs(INDEXES_DIR, exist_ok=True)

    # Create server at runtime (not at import time)
    mcp = create_mcp_server()
    mcp.run(transport="http", port=3000, host="0.0.0.0")


if __name__ == "__main__":
    main()
