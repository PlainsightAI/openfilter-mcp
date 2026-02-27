"""End-to-end tests for token approval flows (elicitation + web fallback).

These tests exercise the full request_scoped_token flow using FastMCP's
in-memory Client, covering both the elicitation path (MCP client supports
interactive dialogs) and the web fallback path (browser-based approval).

Requirements:
    - playwright (dev dependency): ``uv add --dev playwright pytest-playwright``
    - Chromium browser: ``uv run playwright install chromium``
"""

import asyncio
import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.e2e,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Test API", "version": "1.0.0"},
    "paths": {
        "/projects": {
            "get": {
                "operationId": "list_projects",
                "summary": "List all projects",
                "responses": {"200": {"description": "Success"}},
            },
        },
        "/projects/{id}": {
            "get": {
                "operationId": "get_project",
                "summary": "Get a project",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "Success"}},
            },
        },
    },
}

_MOCK_TOKEN_RESPONSE = {
    "id": "tok_test123",
    "token": "ps_test_scoped_token_value",
    "name": "test-token",
}


def _make_mock_client() -> AsyncMock:
    """Create a mock httpx.AsyncClient whose .post() returns a token response."""
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = _MOCK_TOKEN_RESPONSE

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = mock_response
    mock_client.get.return_value = mock_response
    return mock_client


def _make_mock_client_409_then_ok() -> AsyncMock:
    """Mock client that returns 409 on first POST, 200 on retry with disambiguated name."""
    conflict_response = MagicMock(spec=httpx.Response)
    conflict_response.status_code = 409
    conflict_response.json.return_value = {
        "title": "Conflict",
        "status": 409,
        "detail": "api_token: name already exists (entity already exists)",
    }

    ok_response = MagicMock(spec=httpx.Response)
    ok_response.status_code = 200
    ok_response.json.return_value = _MOCK_TOKEN_RESPONSE

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.side_effect = [conflict_response, ok_response]
    return mock_client


@pytest.fixture
def mcp_server() -> FastMCP:
    """Create a FastMCP server with mocked auth, OpenAPI, and HTTP client.

    Patches must remain active through the entire test so that tool calls
    at runtime (e.g. read_psctl_token, get_effective_org_id) still resolve
    to mocked values rather than hitting real auth.
    """
    with (
        patch("openfilter_mcp.server.get_auth_token", return_value="test-token"),
        patch("openfilter_mcp.server.get_openapi_spec", return_value=_MOCK_SPEC),
        patch("openfilter_mcp.server.get_effective_org_id", return_value="test-org"),
        patch("openfilter_mcp.server.get_latest_index_name", return_value="test-index"),
        patch(
            "openfilter_mcp.server.create_authenticated_client",
            return_value=_make_mock_client(),
        ),
        patch("openfilter_mcp.server.read_psctl_token", return_value="test-token"),
    ):
        from openfilter_mcp.server import create_mcp_server

        server = create_mcp_server()
        yield server


@contextmanager
def _make_mcp_server_with_client(mock_client):
    """Create a FastMCP server using a specific mock client.

    Returns a context manager that keeps patches alive for runtime calls
    (read_psctl_token, get_effective_org_id, etc.) during test execution.
    """
    with (
        patch("openfilter_mcp.server.get_auth_token", return_value="test-token"),
        patch("openfilter_mcp.server.get_openapi_spec", return_value=_MOCK_SPEC),
        patch("openfilter_mcp.server.get_effective_org_id", return_value="test-org"),
        patch("openfilter_mcp.server.get_latest_index_name", return_value="test-index"),
        patch(
            "openfilter_mcp.server.create_authenticated_client",
            return_value=mock_client,
        ),
        patch("openfilter_mcp.server.read_psctl_token", return_value="test-token"),
    ):
        from openfilter_mcp.server import create_mcp_server

        yield create_mcp_server()


# ---------------------------------------------------------------------------
# Elicitation flow tests
# ---------------------------------------------------------------------------


async def _approve_handler(message, response_type, params, context):
    """Elicitation handler that auto-approves."""
    return ElicitResult(action="accept", content={"value": "Approve"})


async def _deny_handler(message, response_type, params, context):
    """Elicitation handler that denies."""
    return ElicitResult(action="decline", content=None)


class TestElicitationApprove:
    """Test 1: Elicitation flow — user approves."""

    async def test_approve_returns_active_token(self, mcp_server: FastMCP):
        client = Client(
            mcp_server,
            elicitation_handler=_approve_handler,
        )
        async with client:
            result = await client.call_tool(
                "request_scoped_token",
                {"scopes": "project:read"},
            )

        assert result.data["status"] == "active"
        assert "project:read" in result.data["scopes"]
        assert result.data["token_name"] == "test-token"


class TestElicitationDeny:
    """Test 2: Elicitation flow — user denies."""

    async def test_deny_returns_denied(self, mcp_server: FastMCP):
        client = Client(
            mcp_server,
            elicitation_handler=_deny_handler,
        )
        async with client:
            result = await client.call_tool(
                "request_scoped_token",
                {"scopes": "project:read"},
                raise_on_error=False,
            )

        assert result.data["status"] == "denied"


# ---------------------------------------------------------------------------
# Web fallback flow tests (two-step: return URL, then block on await)
# ---------------------------------------------------------------------------

import socket
from contextlib import asynccontextmanager
import uvicorn
from sse_starlette.sse import AppStatus


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _run_mcp_http(mcp_server: FastMCP, port: int):
    """Run the MCP server on a real HTTP port for the duration of the block.

    Sets the PORT env var so approval URLs use the correct port.
    """
    import os
    old_port = os.environ.get("PORT")
    os.environ["PORT"] = str(port)
    try:
        # Reset stale shutdown flag from any prior uvicorn instance in this
        # process — sse_starlette uses a class-level AppStatus.should_exit
        # that persists across server restarts and kills new SSE connections.
        AppStatus.should_exit = False

        app = mcp_server.http_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        # Wait for server to be ready
        for _ in range(50):
            await asyncio.sleep(0.1)
            if server.started:
                break
        try:
            yield
        finally:
            server.should_exit = True
            await task
    finally:
        if old_port is None:
            os.environ.pop("PORT", None)
        else:
            os.environ["PORT"] = old_port
        # Allow time for port release
        await asyncio.sleep(0.2)


async def _submit_approval(approval_url: str, action: str):
    """Submit an approve/deny response by fetching the page nonce and POSTing the form.

    Uses httpx instead of Playwright — faster and doesn't require a browser install.
    """
    import re

    await asyncio.sleep(0.3)

    async with httpx.AsyncClient() as http:
        # GET the approval page to extract the nonce
        page_resp = await http.get(approval_url)
        assert page_resp.status_code == 200, f"GET {approval_url} returned {page_resp.status_code}"

        # Extract nonce from hidden input
        match = re.search(r'name="nonce" value="([^"]+)"', page_resp.text)
        assert match, "Could not find nonce in approval page"
        nonce = match.group(1)

        # POST the form
        respond_url = f"{approval_url}/respond"
        resp = await http.post(
            respond_url,
            data={"nonce": nonce, "action": action},
            follow_redirects=True,
        )
        assert resp.status_code == 200


class TestWebFallbackApprove:
    """Test 3: Web fallback — request returns URL, agent tells user, await blocks, user approves.

    Uses a real HTTP server + httpx to POST the approval form (faster and more
    reliable than Playwright in CI).
    """

    async def test_approve_via_browser(self, mcp_server: FastMCP):
        port = _find_free_port()

        async with _run_mcp_http(mcp_server, port):
            client = Client(f"http://localhost:{port}/mcp")
            async with client:
                pending = await client.call_tool(
                    "request_scoped_token",
                    {"scopes": "project:read"},
                )

                assert pending.data["status"] == "pending_approval"
                assert "approval_url" in pending.data
                assert "request_id" in pending.data

                url = pending.data["approval_url"]
                request_id = pending.data["request_id"]

                # Submit approval via HTTP POST (simulates browser form submit)
                approve_task = asyncio.create_task(
                    _submit_approval(url, "approve")
                )

                result = await client.call_tool(
                    "await_token_approval",
                    {"request_id": request_id},
                )

                await approve_task

            assert result.data["status"] == "active"
            assert "project:read" in result.data["scopes"]


class TestWebFallbackDeny:
    """Test 4: Web fallback — user denies via browser."""

    async def test_deny_via_browser(self, mcp_server: FastMCP):
        port = _find_free_port()

        async with _run_mcp_http(mcp_server, port):
            client = Client(f"http://localhost:{port}/mcp")
            async with client:
                pending = await client.call_tool(
                    "request_scoped_token",
                    {"scopes": "project:read"},
                )

                assert pending.data["status"] == "pending_approval"
                url = pending.data["approval_url"]
                request_id = pending.data["request_id"]

                deny_task = asyncio.create_task(
                    _submit_approval(url, "deny")
                )

                result = await client.call_tool(
                    "await_token_approval",
                    {"request_id": request_id},
                    raise_on_error=False,
                )

                await deny_task

            assert result.data["status"] == "denied"


class TestWebFallbackTimeout:
    """Test 5: Web fallback — approval session times out."""

    async def test_timeout_returns_denied(self):
        """Test ApprovalRegistry timeout directly (no HTTP needed)."""
        from openfilter_mcp.approval_server import ApprovalRegistry

        registry = ApprovalRegistry()
        session = registry.create_session(
            title="Test",
            message="Test timeout",
            details={"scope": ["project:read"]},
            timeout_seconds=1,
        )

        result = await session.wait()
        assert result == "timeout"


class TestWebFallbackInvalidRequestId:
    """Test 6: await_token_approval with bad request_id."""

    async def test_invalid_request_id(self, mcp_server: FastMCP):
        client = Client(mcp_server)

        async with client:
            result = await client.call_tool(
                "await_token_approval",
                {"request_id": "nonexistent"},
                raise_on_error=False,
            )

        assert "error" in result.data
        assert "No pending approval" in result.data["error"]


# ---------------------------------------------------------------------------
# Token conflict (409) and session-prefixed naming tests
# ---------------------------------------------------------------------------


class TestTokenConflict409:
    """Test: request_scoped_token handles 409 by retrying with a disambiguated name."""

    async def test_409_conflict_resolved_by_retry_with_new_name(self):
        mock_client = _make_mock_client_409_then_ok()
        with _make_mcp_server_with_client(mock_client) as server:
            client = Client(server, elicitation_handler=_approve_handler)
            async with client:
                result = await client.call_tool(
                    "request_scoped_token",
                    {"scopes": "project:read"},
                )

        assert result.data["status"] == "active"
        # POST was called twice (409 then retry with disambiguated name)
        assert mock_client.post.call_count == 2


class TestTokenNameIsSessionPrefixed:
    """Test: default token name includes a session-unique prefix."""

    async def test_default_name_contains_mcp_prefix(self):
        mock_client = _make_mock_client()
        with _make_mcp_server_with_client(mock_client) as server:
            client = Client(server, elicitation_handler=_approve_handler)
            async with client:
                result = await client.call_tool(
                    "request_scoped_token",
                    {"scopes": "project:read"},
                )

        assert result.data["status"] == "active"
        # Verify the POST payload used a name starting with "mcp-"
        post_call = mock_client.post.call_args
        payload = post_call.kwargs.get("json") or post_call[1].get("json")
        assert payload["name"].startswith("mcp-")


class TestClearScopedToken:
    """Test: clear_scoped_token clears local session state."""

    async def test_clear_returns_cleared(self, mcp_server: FastMCP):
        client = Client(mcp_server, elicitation_handler=_approve_handler)
        async with client:
            # First create a scoped token
            create_result = await client.call_tool(
                "request_scoped_token",
                {"scopes": "project:read"},
            )
            assert create_result.data["status"] == "active"

            # Now clear it
            clear_result = await client.call_tool("clear_scoped_token", {})

        assert clear_result.data["status"] == "cleared"

    async def test_clear_without_active_token(self, mcp_server: FastMCP):
        client = Client(mcp_server, elicitation_handler=_approve_handler)
        async with client:
            result = await client.call_tool("clear_scoped_token", {})

        assert result.data["status"] == "no_change"


# ---------------------------------------------------------------------------
# Delta scope mode tests (add_scopes / remove_scopes)
# ---------------------------------------------------------------------------


class TestDeltaScopeMode:
    """Test: request_scoped_token with add_scopes/remove_scopes delta mode."""

    async def test_add_scopes_to_existing_token(self, mcp_server: FastMCP):
        """Create a token, then use add_scopes to expand permissions."""
        client = Client(mcp_server, elicitation_handler=_approve_handler)
        async with client:
            # First create a base token
            result1 = await client.call_tool(
                "request_scoped_token",
                {"scopes": "project:read"},
            )
            assert result1.data["status"] == "active"

            # Now add a scope using delta mode
            result2 = await client.call_tool(
                "request_scoped_token",
                {"add_scopes": "project:create"},
            )
            assert result2.data["status"] == "active"
            assert "project:read" in result2.data["scopes"]
            assert "project:create" in result2.data["scopes"]

    async def test_remove_scopes_from_existing_token(self, mcp_server: FastMCP):
        """Create a token with multiple scopes, then remove one."""
        client = Client(mcp_server, elicitation_handler=_approve_handler)
        async with client:
            result1 = await client.call_tool(
                "request_scoped_token",
                {"scopes": "project:read,project:create"},
            )
            assert result1.data["status"] == "active"

            result2 = await client.call_tool(
                "request_scoped_token",
                {"remove_scopes": "project:create"},
            )
            assert result2.data["status"] == "active"
            assert "project:read" in result2.data["scopes"]
            assert "project:create" not in result2.data["scopes"]

    async def test_delta_empty_result_returns_error(self, mcp_server: FastMCP):
        """Removing all scopes via delta mode returns an error."""
        client = Client(mcp_server, elicitation_handler=_approve_handler)
        async with client:
            result1 = await client.call_tool(
                "request_scoped_token",
                {"scopes": "project:read"},
            )
            assert result1.data["status"] == "active"

            result2 = await client.call_tool(
                "request_scoped_token",
                {"remove_scopes": "project:read"},
                raise_on_error=False,
            )
            assert "error" in result2.data

    async def test_no_scopes_param_returns_error(self, mcp_server: FastMCP):
        """Calling with neither scopes nor add_scopes/remove_scopes returns error."""
        client = Client(mcp_server, elicitation_handler=_approve_handler)
        async with client:
            result = await client.call_tool(
                "request_scoped_token",
                {},
                raise_on_error=False,
            )
            assert "error" in result.data


# ---------------------------------------------------------------------------
# Empty scopes validation tests
# ---------------------------------------------------------------------------


class TestEmptyScopesValidation:
    """Test: scopes="" produces an error, not a zero-permission token."""

    async def test_empty_scopes_string_returns_error(self, mcp_server: FastMCP):
        client = Client(mcp_server, elicitation_handler=_approve_handler)
        async with client:
            result = await client.call_tool(
                "request_scoped_token",
                {"scopes": ""},
                raise_on_error=False,
            )
            assert "error" in result.data
            assert "No scopes" in result.data["error"]

    async def test_whitespace_only_scopes_returns_error(self, mcp_server: FastMCP):
        client = Client(mcp_server, elicitation_handler=_approve_handler)
        async with client:
            result = await client.call_tool(
                "request_scoped_token",
                {"scopes": " , , "},
                raise_on_error=False,
            )
            assert "error" in result.data


# ---------------------------------------------------------------------------
# _recreate_expired_token via approval_registry tests
# ---------------------------------------------------------------------------


class TestRecreateExpiredTokenApproval:
    """Test: _recreate_expired_token uses approval_registry when elicitation fails."""

    async def test_recreate_via_approval_registry(self):
        """When elicitation is unsupported, _recreate_expired_token falls back to approval_registry."""
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData
        from openfilter_mcp.approval_server import ApprovalRegistry
        from openfilter_mcp.entity_tools import EntityToolsHandler, EntityRegistry

        registry = ApprovalRegistry()
        mock_client = _make_mock_client()
        entity_registry = EntityRegistry({
            "openapi": "3.1.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {"/projects": {"get": {"operationId": "list_projects", "summary": "List", "responses": {"200": {"description": "OK"}}}}},
        })
        handler = EntityToolsHandler(mock_client, entity_registry, approval_registry=registry)

        scoped_meta = {
            "name": "test-renewal",
            "scopes": ["project:read"],
            "org_id": "test-org",
        }

        # Mock ctx that raises McpError on elicit (simulates unsupported client)
        ctx = AsyncMock()
        ctx.info = AsyncMock()
        ctx.set_state = AsyncMock()
        ctx.get_state = AsyncMock(return_value=None)
        ctx.elicit = AsyncMock(
            side_effect=McpError(ErrorData(code=-32601, message="Method not found"))
        )

        # Approve in background after a short delay
        async def approve_after_delay():
            await asyncio.sleep(0.5)
            # Get the session_id and nonce from the registry internals
            session_ids = list(registry._sessions.keys())
            assert len(session_ids) == 1
            session_id = session_ids[0]
            entry = registry._sessions[session_id]
            registry.submit_response(session_id, entry["nonce"], "approve")

        approve_task = asyncio.create_task(approve_after_delay())
        new_token = await handler._recreate_expired_token(scoped_meta, ctx)
        await approve_task

        assert new_token == _MOCK_TOKEN_RESPONSE["token"]

