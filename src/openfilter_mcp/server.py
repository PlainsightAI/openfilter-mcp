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
from openfilter_mcp.scopes import (
    ScopesUnavailable,
    classify_rejection,
    get_or_fetch_grantable,
    is_scope_granted,
    suggest_grantable,
)

logger = logging.getLogger(__name__)

# Session state keys are imported from entity_tools for consistency

# code-context is an optional dependency (install with `uv sync --group code-search`)
try:
    from code_context.indexing import INDEXES_DIR  # pyright: ignore[reportMissingImports]
    from code_context.main import _get_chunk, _search_index  # pyright: ignore[reportMissingImports]

    HAS_CODE_CONTEXT = True
except ImportError:
    INDEXES_DIR = None  # pyright: ignore[reportConstantRedefinition]
    HAS_CODE_CONTEXT = False  # pyright: ignore[reportConstantRedefinition]


# =============================================================================
# OpenAPI Spec Loading
# =============================================================================


def sanitize_openapi_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Sanitize OpenAPI spec for MCP compatibility.

    Removes properties with invalid names (MCP requires ^[a-zA-Z0-9_-]{1,64}$).

    Args:
        spec: The raw OpenAPI specification.

    Returns:
        The sanitized OpenAPI specification.
    """
    import re

    valid_pattern = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

    def clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
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


def get_openapi_spec() -> dict[str, Any]:
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


def get_entity_spec() -> dict[str, Any] | None:
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


def create_authenticated_client(timeout: float = 30.0, *, require_token: bool = False):
    """Create an async HTTP client for Plainsight API.

    Auth resolution model:

      1. If a startup token is available (psctl token file or
         OPENFILTER_TOKEN env), bind it as the client's default
         Authorization header.
      2. If not, return a client with NO Authorization default. Per-
         request handlers (entity_tools._get_request_headers,
         request_scoped_token's bootstrap path) override Authorization
         on each call from session-scoped state, the user's primary
         token, or the FastMCP-context OAuth bearer (whichever is
         appropriate for that specific call). This is the load-bearing
         change for OAuth-only deployments where there's no startup
         token at all but every authenticated request carries a bearer.

    Supports cross-tenant operations for Plainsight employees via
    PS_TARGET_ORG_ID.

    Args:
        timeout: Request timeout in seconds.
        require_token: If True, raise AuthenticationError when no
            startup token is available (legacy behavior; only set this
            from explicit psctl-only deployment paths). Defaults to
            False so OAuth-only mode constructs a no-default-auth
            client and tools register normally.

    Returns:
        Configured httpx.AsyncClient instance with schema-stripping
        middleware and automatic 401 retry via token refresh.

    Raises:
        AuthenticationError: If `require_token=True` and no token is
            available.
    """
    token = get_auth_token()
    if not token and require_token:
        raise AuthenticationError("No authentication token available")

    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
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
1. Determine which entity types and actions your task needs (use `list_entity_types` to discover available resources; use `list_grantable_scopes` to enumerate the exact scope strings your role can grant, including sub-actions like `start`/`stop`/`trigger`).
2. Call `request_scoped_token` with a comma-separated list of "resource:action" scopes.
   - The user will be asked to approve via an interactive dialog or browser page.
   - If the response includes an `approval_url`, tell the user to open it in their browser,
     then call `await_token_approval` with the `request_id` to block until they respond.
3. Once approved, the scoped token is stored in the session and used automatically for all subsequent API calls.
4. Use `get_token_status` to check current permissions and expiry.
5. Use `clear_scoped_token` to revert to the default token.

Scope format: "resource:action" where resource is any entity type (or * for all resources) and action is drawn from the live policies — read, create, update, delete, plus per-domain sub-actions like start/stop/trigger/upload where applicable. Validation is driven by `/rbac/scopes`, so call `list_grantable_scopes` to see the authoritative set for your role. Examples: "project:read,filterpipeline:read", "filterpipeline:*,pipelineinstance:*", "pipelineinstance:start,pipelineinstance:stop", "*:*" (admin only).

IMPORTANT: Resource names and entity_type values are lowercase with NO underscores or hyphens. Use 'filterpipeline' (not 'filter_pipeline'), 'pipelineinstance' (not 'pipeline_instance'), 'sourceconfig' (not 'source_config'). Use list_entity_types() to discover valid names.

When planning scopes, think ahead about the FULL task. If the user asks to "see what's running in my project," you'll need project + filterpipeline + pipelineinstance + filter access — request all of these upfront in a single request_scoped_token call rather than escalating incrementally. This reduces approval fatigue. Only escalate if a genuinely unexpected need arises (e.g., the user asks you to modify something after a read-only investigation).

Escalation via delta: When you need additional scopes beyond what you already have, use `add_scopes` instead of repeating the full set. Example: `request_scoped_token(add_scopes="filterpipeline:update")` to add write access while keeping existing read scopes. Use `remove_scopes` to drop scopes you no longer need. The server merges the delta with your current scopes and presents the full result for approval.

Choosing the right scope breadth — security vs. autonomy:

Narrower scopes protect against accidental modifications, but overly narrow scopes cause repeated approval prompts that slow the user down and create approval fatigue. The right scope set depends on the task:

- **Quick, focused task** (e.g., "check if pipeline X is running"): request only the specific scopes needed — `filterpipeline:read,pipelineinstance:read`.
- **Broad investigation or long-running session** (e.g., "audit everything in my project"): request wider read scopes upfront — a broad set of per-resource reads like `project:read,filterpipeline:read,pipelineinstance:read,...` — so you can explore freely without re-prompting. (There is no `*:read` shorthand; `/rbac/scopes` only emits per-resource wildcards like `filterpipeline:*` or the admin-only `*:*`.)
- **Tasks likely to involve writes**: if the user's intent clearly implies modifications (e.g., "fix the misconfigured pipeline"), request both read AND write scopes for the relevant resources upfront rather than forcing a separate escalation.

As a guideline: prefer the scope set that lets you complete the user's stated goal with a single approval. Avoid broad wildcard grants like `*:*` or per-resource writes (`filterpipeline:*`) when concrete actions would do. When in doubt, lean toward slightly broader read scopes and narrower write scopes — reads are low-risk, writes deserve more scrutiny.

Tool usage tips:
- list_entities uses `filters` (not `query_params`) for HTTP query parameters. Example: list_entities('filterpipeline', filters={'project': '<id>'}).
- Most list endpoints filter by project via query params, not path params. Check get_entity_type_info() to see which params go where.
- get_entity uses `id` (not `entity_id`) as the parameter name.

Note: By default, all API operations require a scoped token. You MUST call request_scoped_token before any entity operations. This requirement can be relaxed by setting OPF_MCP_ALLOW_UNSCOPED_TOKEN=true, but this is not recommended.
""".strip()


def _build_oauth_provider() -> Any:
    """Construct a FastMCP RemoteAuthProvider when OAUTH_AS_URL is set,
    otherwise return None and let the existing psctl-token Bearer path
    (auth.py) handle requests as before.

    The provider does two things FastMCP wires up automatically once
    `auth=` is non-None:

      1. Mounts `/.well-known/oauth-protected-resource` (RFC 9728) so
         MCP clients discover the AS by querying *this* server first
         rather than the cascade of fallbacks they currently 404 on
         when they hit a server without this endpoint.
      2. Returns 401 with a `WWW-Authenticate: Bearer ... resource_metadata="<url>"`
         header on any unauthenticated request, pointing the client at
         the protected-resource document above.

    Token validation: ES256 (ECDSA P-256) JWTs against the AS's JWKS.
    Plainsight API (DT-132) issues these — see internal/oauth/service/
    keystore.go for the JWK shape we consume here. EdDSA was rejected
    upstream specifically because FastMCP's stock JWTVerifier hardcodes
    its alg allowlist and excludes it; ES256 is in the allowlist.

    Audience: RFC 8707 resource-indicator-aware MCP clients (Claude Code,
    MCP Inspector) pass `resource=<this-server>/mcp` at /authorize time,
    and plainsight-api binds the JWT `aud` claim to that resource. So we
    accept BOTH `<resource_url>/mcp` (the spec'd binding) AND `<as_url>`
    (the iss-fallback for older clients that don't pass resource=). The
    JWTVerifier accepts a list of audiences and validates that the token's
    aud matches at least one. OAUTH_AUDIENCE overrides this list entirely
    if the deployment needs custom values.
    """
    as_url = os.getenv("OAUTH_AS_URL")
    if not as_url:
        return None

    # Imports are local so the existing psctl-token deployment path
    # doesn't pull in the auth provider chain it never uses.
    from fastmcp.server.auth import RemoteAuthProvider  # type: ignore[import-not-found]
    from fastmcp.server.auth.providers.jwt import JWTVerifier  # type: ignore[import-not-found]

    as_url = as_url.rstrip("/")
    resource_url = os.getenv(
        "OAUTH_RESOURCE_URL",
        f"http://localhost:{os.getenv('PORT', '3000')}",
    ).rstrip("/")
    # Default audience set: the spec'd RFC 8707 binding (resource_url +
    # "/mcp", which is what the protected-resource doc advertises and
    # what RemoteAuthProvider broadcasts as the resource) plus the AS
    # URL for iss-fallback compatibility. Either passes verification.
    audience_env = os.getenv("OAUTH_AUDIENCE")
    if audience_env:
        audience: str | list[str] = [a.strip() for a in audience_env.split(",") if a.strip()]
    else:
        audience = [f"{resource_url}/mcp", as_url]

    verifier = JWTVerifier(
        jwks_uri=f"{as_url}/.well-known/jwks.json",
        issuer=as_url,
        audience=audience,
        algorithm="ES256",
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[as_url],
        base_url=resource_url,
        resource_name="OpenFilter MCP",
    )


def create_mcp_server() -> FastMCP:
    """Create the MCP server with entity-based API tools and code search tools.

    Returns:
        A FastMCP server instance with entity CRUD tools plus code search tools.
        If no authentication token is available, only code search tools are registered.

    Raises:
        SystemExit: If REQUIRE_AUTH is set and no valid token is found.
    """
    # Create base MCP server. `auth` is None unless OAUTH_AS_URL is set,
    # in which case FastMCP mounts the RFC 9728 protected-resource
    # endpoints + 401 WWW-Authenticate dance automatically.
    mcp = FastMCP(
        name="OpenFilter MCP",
        instructions=_SERVER_INSTRUCTIONS,
        auth=_build_oauth_provider(),
    )

    # Register approval routes on the MCP server (reuses port 3000, works in Docker)
    approval_registry = register_approval_routes(mcp)

    # When REQUIRE_AUTH is set (e.g. slim Docker images that have no code-search
    # fallback), refuse to start if we cannot authenticate -- an unauthenticated
    # slim server would register zero tools and silently do nothing.
    require_auth = os.getenv("REQUIRE_AUTH", "false").lower() == "true"

    # OpenAPI + entity-spec are PUBLIC endpoints on plainsight-api — they
    # don't require authentication. Fetch them unconditionally so entity
    # tools can register regardless of whether a startup credential is
    # available. Auth is resolved per-request inside the tool handlers
    # (session-scoped token from elicitation > psctl/env startup token >
    # FastMCP-context OAuth bearer for the request_scoped_token bootstrap
    # path), not at registration time.
    #
    # Earlier this was gated on `if token:` AND wrapped in a swallow-the-
    # exception block, so a deployment with no psctl token (OAuth-only)
    # silently registered zero tools — Claude Code saw an empty catalog
    # via tools/list and looked broken end-to-end with no log line
    # explaining why. Decoupling here also surfaces openapi-fetch
    # failures loudly via the regular httpx exception path instead of
    # hiding them behind has_auth=False.
    token = get_auth_token()
    client = None
    openapi_spec = None
    has_auth = bool(token)
    auth_mode = (
        "psctl/env token"
        if token
        else "OAuth-only (per-request bearer)" if os.getenv("OAUTH_AS_URL") else "none"
    )

    try:
        openapi_spec = get_openapi_spec()
    except Exception as exc:
        logger.warning(
            "openapi.json fetch failed at startup against %s — entity tools will not register: %s",
            PLAINSIGHT_API_URL,
            exc,
        )

    # Construct an http client even when there's no startup token — it's
    # used by entity tool handlers per-request with session-scoped
    # Authorization headers, and by request_scoped_token's bootstrap path
    # (which resolves Authorization at call time, not at client
    # construction).
    try:
        client = create_authenticated_client(require_token=False)
    except AuthenticationError as exc:
        # require_token=False shouldn't raise on a missing token, so an
        # AuthenticationError here means something deeper went wrong
        # (e.g. transport refused to mint a default-headers client).
        # Honor the documented REQUIRE_AUTH fail-fast contract.
        if require_auth:
            raise SystemExit(
                f"REQUIRE_AUTH is set but authentication failed: {exc}\n"
                "Provide a valid token via OPENFILTER_TOKEN env var, mount a "
                "psctl token file, or enable OAuth-only mode by setting "
                "OAUTH_AS_URL."
            ) from exc
        logger.warning("create_authenticated_client raised unexpectedly: %s", exc)

    if require_auth and not has_auth and auth_mode != "OAuth-only (per-request bearer)":
        raise SystemExit(
            "REQUIRE_AUTH is set but no authentication path is available.\n"
            "Provide a valid token via OPENFILTER_TOKEN env var, mount a psctl "
            "token file (~/.config/plainsight/token), or enable OAuth-only "
            "mode by setting OAUTH_AS_URL."
        )

    if not has_auth:
        if auth_mode == "OAuth-only (per-request bearer)":
            logger.info(
                "no startup credential found; running in OAuth-only mode "
                "(per-request bearer auth via OAUTH_AS_URL=%s)",
                os.getenv("OAUTH_AS_URL"),
            )
        else:
            logger.warning(
                "no startup credential AND no OAUTH_AS_URL — entity ops "
                "will fail at request time with PermissionError. Set "
                "OPENFILTER_TOKEN or run `psctl login`."
            )

    # Register entity-based CRUD tools whenever the OpenAPI spec is
    # available — auth is no longer the registration gate. The handlers
    # themselves enforce the elicitation gate (session-scoped token
    # required for entity ops) at runtime.
    registry = None
    entity_handler = None
    if openapi_spec and client is not None:
        entity_spec = get_entity_spec()
        registry, entity_handler = register_entity_tools(
            mcp, client, openapi_spec, entity_spec=entity_spec, approval_registry=approval_registry
        )

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
            return _search_index(latest_index_name, query, "nl2code", top_k)  # pyright: ignore[reportPossiblyUnboundVariable]

        @mcp.tool()
        def search_code(code_query: str, top_k: int = 10) -> Dict[str, Any]:
            """Searches a semantic index for code similar to the provided code snippet.

            Returns the top_k most relevant chunks with their scores and metadata."""
            return _search_index(latest_index_name, code_query, "code2code", top_k)  # pyright: ignore[reportPossiblyUnboundVariable]

        @mcp.tool()
        def get_chunk(chunk_id: int) -> Dict[str, Any]:
            """Retrieves the content and metadata of a specific chunk by its ID.

            Returns: JSON object with filepath, startLine, endLine, and content."""
            return _get_chunk(latest_index_name, chunk_id)  # pyright: ignore[reportPossiblyUnboundVariable]

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
    # Generic Polling Tool (only registered when openapi succeeded — its
    # implementation calls back into entity handlers which themselves
    # enforce per-request auth resolution).
    # =========================================================================

    if client is not None and entity_handler is not None:

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

    def _resolve_bootstrap_auth() -> str | None:
        """Resolve the credential to use for the /api-tokens bootstrap call.

        Order of preference:

          1. `get_auth_token()` — psctl token file or OPENFILTER_TOKEN env.
             Long-lived primary credential when present.
          2. FastMCP request-context OAuth bearer — the user's just-
             OAuth-approved access token, available in OAuth-only
             deployments where (1) is empty.

        Returns the raw bearer string or None if neither path has a
        credential. Used ONLY by `_create_and_activate_token` for the
        /api-tokens elicitation-bootstrap call. Entity ops do NOT
        consume this — they go through `entity_tools._get_request_headers`
        which requires a session-scoped token (the elicitation gate).
        That gate stays intact regardless of which bootstrap credential
        was used here.
        """
        token = get_auth_token()
        if token:
            return token
        # Fall through to the OAuth bearer if we're running OAuth-only.
        # Importing the dependency here keeps the existing psctl-only
        # deployment path from pulling in the FastMCP auth module.
        try:
            from fastmcp.server.dependencies import get_access_token  # type: ignore[import-not-found]
            access = get_access_token()
            if access is not None:
                return access.token
        except Exception:
            pass
        return None

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
        # /api-tokens needs a credential. The startup-bound client may
        # have no default Authorization header (OAuth-only mode); fall
        # back to the request bearer if so. Either way the resulting
        # session-scoped token is what subsequent entity ops use, so the
        # elicitation gate is preserved.
        bootstrap_token = _resolve_bootstrap_auth()
        org_headers = {"X-Scope-OrgID": effective_org_id}
        if bootstrap_token:
            org_headers["Authorization"] = f"Bearer {bootstrap_token}"
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

    # Pending web-based approvals: session_id -> {request_id, session, scope_list, name, ...}
    # At most one pending approval per MCP session — a new request auto-cancels the previous.
    _pending_approvals: Dict[str, Any] = {}  # keyed by ctx.session_id

    # Scoped-token tools register whenever an http client exists — auth
    # for the bootstrap /api-tokens call resolves at request time via
    # _resolve_bootstrap_auth (psctl/env > FastMCP-context OAuth bearer).
    # The elicitation gate (session-bound scoped token required for
    # entity ops) lives downstream in entity_tools._get_request_headers
    # and is unaffected.
    if client is not None:

        @mcp.tool()
        async def list_grantable_scopes(ctx: Context | None = None) -> Dict[str, Any]:
            """List the RBAC scopes the current caller can grant when creating
            an API token via request_scoped_token.

            The list is fetched from plainsight-api's GET /rbac/scopes (role-
            filtered to your Casbin role) and cached for this MCP session.
            Call this before request_scoped_token if you're unsure which scope
            strings are valid for your role — avoids trial-and-error validation
            errors and picks up new sub-action scopes (e.g., 'start', 'trigger')
            without an MCP release.

            Returns:
                {"scopes": ["filterpipeline:read", "filterpipeline:*", ...]}
                sorted. Or {"error": ...} if /rbac/scopes is unreachable.
            """
            if not ctx:
                return {"error": "This tool requires a session context. Ensure your MCP client provides one."}
            try:
                grantable = await get_or_fetch_grantable(ctx, client)
            except ScopesUnavailable as e:
                return {"error": f"/rbac/scopes unavailable (status={e.status}): {e.detail}"}
            return {"scopes": sorted(grantable)}

        @mcp.tool()
        async def request_scoped_token(
            scopes: str | None = None,
            add_scopes: str | None = None,
            remove_scopes: str | None = None,
            name: str = f"mcp-{_session_id}",
            expires_in_hours: int = 1,
            org_id: str | None = None,
            ctx: Context | None = None,
        ) -> Dict[str, Any]:
            """Request a scoped API token with limited permissions for this session.

            This creates a new API token with only the permissions you need,
            replacing the current token for subsequent API calls. The user will
            be asked to approve the requested scopes via an interactive dialog
            (elicitation). If your MCP client does not support elicitation, a
            browser-based approval page is started instead and the tool returns
            immediately with an approval_url and request_id. You MUST tell the
            user to open the URL, then call await_token_approval with the
            request_id to block until they respond.

            You will NOT see the token value — it is stored securely in the
            session and used automatically for subsequent API calls.

            There are two ways to specify scopes:

            1. **Absolute** — pass `scopes` with the full set you need.
               This replaces any existing token entirely.

            2. **Delta** — pass `add_scopes` and/or `remove_scopes` to modify
               the current token's scopes. The server reads the active token's
               scopes, applies the delta, and requests approval for the result.
               This avoids repeating scopes you already have and makes
               escalation intent clear. You do NOT need to call
               clear_scoped_token first when using delta mode.

            If both `scopes` and `add_scopes`/`remove_scopes` are provided,
            `scopes` takes priority (delta params are ignored).

            Args:
                scopes: Comma-separated list of permission scopes to request
                    (replaces all current scopes). Format: "resource:action",
                    validated against the caller's grantable set from
                    plainsight-api's /rbac/scopes (so sub-action scopes like
                    'start', 'stop', 'trigger', 'upload' are accepted if your
                    role covers them). Call list_grantable_scopes() to
                    enumerate exactly what you can request — beats guessing.
                    IMPORTANT: Resource names are lowercase with NO underscores
                    or hyphens — e.g., 'filterpipeline' not 'filter_pipeline',
                    'pipelineinstance' not 'pipeline_instance'.
                    Examples: "project:read,filterpipeline:read,pipelineinstance:read",
                             "filterpipeline:*,pipelineinstance:*",
                             "*:*" (full access; admin roles only).
                add_scopes: Comma-separated scopes to ADD to the current token.
                    Example: "filterpipeline:update" to add write access while
                    keeping existing read scopes.
                remove_scopes: Comma-separated scopes to REMOVE from the current
                    token. Example: "filter:read" to drop a scope you no longer need.
                name: A name for this token (for identification in the portal).
                expires_in_hours: How long the token should be valid (default: 1 hour).
                org_id: Optional organization ID to create the token for. If not provided,
                    the org ID is derived from the current psctl/auth token.

            Returns:
                Confirmation of which scopes were granted (token value is hidden).
            """
            if not ctx:
                return {"error": "This tool requires a session context. Ensure your MCP client provides one."}

            # Resolve scope list: absolute mode vs delta mode
            if scopes is not None:
                # Absolute mode — use exactly what was provided
                scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
                if not scope_list:
                    return {"error": "No scopes provided. Specify at least one scope like 'project:read'."}
            elif add_scopes is not None or remove_scopes is not None:
                # Delta mode — read current scopes and apply changes
                meta = await ctx.get_state(SESSION_TOKEN_META_KEY)
                current = set(meta.get("scopes", [])) if meta else set()

                to_add = {s.strip() for s in (add_scopes or "").split(",") if s.strip()}
                to_remove = {s.strip() for s in (remove_scopes or "").split(",") if s.strip()}

                scope_list = sorted((current | to_add) - to_remove)

                if not scope_list:
                    return {"error": "Resulting scope set is empty after applying delta. "
                            f"Current: {sorted(current)}, add: {sorted(to_add)}, remove: {sorted(to_remove)}"}
            else:
                return {"error": "Provide either 'scopes' (absolute) or 'add_scopes'/'remove_scopes' (delta)."}

            try:
                grantable = await get_or_fetch_grantable(ctx, client)
            except ScopesUnavailable as e:
                return {"error": (
                    f"Cannot validate requested scopes: /rbac/scopes is unavailable "
                    f"(status={e.status}): {e.detail}. Refusing to approve token "
                    f"creation against an unverified scope set. Retry after "
                    f"plainsight-api recovers."
                )}

            errors = []
            for s in scope_list:
                if is_scope_granted(s, grantable):
                    continue
                # classify_rejection splits "unknown resource X" from "unknown
                # action Y for resource Z" so agents can fix the wrong half
                # directly instead of guessing from a generic rejection.
                reason = classify_rejection(s, grantable)
                hint = suggest_grantable(s, grantable)
                if hint:
                    errors.append(f"{s} ({reason}; did you mean '{hint}'?)")
                else:
                    errors.append(f"{s} ({reason})")
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

                # Compute org_id now so it's available when the approval completes.
                # Use _resolve_bootstrap_auth so OAuth-only deployments (no psctl
                # token) can still derive org_id from the request bearer's flat
                # `org_id` claim (DT-132).
                effective_org_id = org_id
                if not effective_org_id:
                    token = _resolve_bootstrap_auth()
                    if not token:
                        return {"error": "No authentication token available."}
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
                    base_url=os.getenv('MCP_BASE_URL', f"http://localhost:{os.getenv('PORT', '3000')}"),
                )

                # One pending approval per session — cancel any previous one
                mcp_session_id = ctx.session_id
                existing = _pending_approvals.get(mcp_session_id)
                if existing:
                    existing["session"].cancel("cancelled")
                    logger.info("Cancelled previous pending approval for session %s", mcp_session_id)

                # Store pending state so await_token_approval can finalize
                import secrets
                request_id = secrets.token_urlsafe(8)
                _pending_approvals[mcp_session_id] = {
                    "request_id": request_id,
                    "session": session,
                    "scope_list": scope_list,
                    "name": name,
                    "expires_at": expires_at,
                    "effective_org_id": effective_org_id,
                }

                # Clean up stale entry if the user never calls await_token_approval
                session.add_timeout_callback(lambda sid=mcp_session_id: _pending_approvals.pop(sid, None))

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

            # Get the effective org ID. _resolve_bootstrap_auth covers both the
            # legacy psctl/env path and the OAuth-only request-bearer fallback.
            effective_org_id = org_id
            if not effective_org_id:
                token = _resolve_bootstrap_auth()
                if not token:
                    return {"error": "No authentication token available."}
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

            # Look up by request_id across all sessions
            mcp_session_id = next(
                (sid for sid, p in _pending_approvals.items() if p["request_id"] == request_id),
                None,
            )
            pending = _pending_approvals.get(mcp_session_id) if mcp_session_id else None
            if not pending:
                return {"error": f"No pending approval found for request_id '{request_id}'. "
                        "It may have already been completed or expired."}

            session = pending["session"]

            # Block until the user responds or timeout
            result = await session.wait()

            # Clean up pending state (may already be removed by timeout callback)
            _pending_approvals.pop(mcp_session_id, None)

            if result == "cancelled":
                return {"status": "cancelled", "message": "A new token request superseded this one."}
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
        assert INDEXES_DIR is not None
        os.makedirs(INDEXES_DIR, exist_ok=True)

    # Create server at runtime (not at import time)
    mcp = create_mcp_server()
    port = int(os.getenv("PORT", "3000"))
    mcp.run(transport="http", port=port, host="0.0.0.0")


if __name__ == "__main__":
    main()
