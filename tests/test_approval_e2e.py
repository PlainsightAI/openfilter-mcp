"""End-to-end tests for token approval flows (elicitation + web fallback).

These tests exercise the full request_scoped_token flow using FastMCP's
in-memory Client, covering both the elicitation path (MCP client supports
interactive dialogs) and the web fallback path (browser-based approval).

Requirements:
    - playwright (dev dependency): ``uv add --dev playwright pytest-playwright``
    - Chromium browser: ``uv run playwright install chromium``
"""

import asyncio
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


@pytest.fixture
def mcp_server() -> FastMCP:
    """Create a FastMCP server with mocked auth, OpenAPI, and HTTP client."""
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
    return server


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


async def _click_button_when_ready(url: str, button_text: str):
    """Launch a headless browser, navigate to the approval URL, and click a button.

    Runs concurrently with the blocking await_token_approval call.
    """
    from playwright.async_api import async_playwright

    # Small delay to ensure the approval server is listening
    await asyncio.sleep(0.3)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url)
        await page.click(f"button:has-text('{button_text}')")
        await browser.close()


class TestWebFallbackApprove:
    """Test 3: Web fallback — request returns URL, agent tells user, await blocks, user approves."""

    async def test_approve_via_browser(self, mcp_server: FastMCP):
        # No elicitation handler → server will fall back to web approval
        client = Client(mcp_server)

        async with client:
            # Step 1: request_scoped_token returns immediately with pending_approval
            pending = await client.call_tool(
                "request_scoped_token",
                {"scopes": "project:read"},
            )

            assert pending.data["status"] == "pending_approval"
            assert "approval_url" in pending.data
            assert "request_id" in pending.data

            url = pending.data["approval_url"]
            request_id = pending.data["request_id"]

            # Step 2: Start browser click concurrently, then block on await_token_approval
            click_task = asyncio.create_task(_click_button_when_ready(url, "Approve"))

            result = await client.call_tool(
                "await_token_approval",
                {"request_id": request_id},
            )

            await click_task

        assert result.data["status"] == "active"
        assert "project:read" in result.data["scopes"]


class TestWebFallbackDeny:
    """Test 4: Web fallback — user denies via browser."""

    async def test_deny_via_browser(self, mcp_server: FastMCP):
        client = Client(mcp_server)

        async with client:
            pending = await client.call_tool(
                "request_scoped_token",
                {"scopes": "project:read"},
            )

            assert pending.data["status"] == "pending_approval"
            url = pending.data["approval_url"]
            request_id = pending.data["request_id"]

            click_task = asyncio.create_task(_click_button_when_ready(url, "Deny"))

            result = await client.call_tool(
                "await_token_approval",
                {"request_id": request_id},
                raise_on_error=False,
            )

            await click_task

        assert result.data["status"] == "denied"


class TestWebFallbackTimeout:
    """Test 5: Web fallback — approval server times out."""

    async def test_timeout_returns_denied(self, mcp_server: FastMCP):
        from openfilter_mcp.approval_server import start_approval_server as real_start

        async def short_timeout_start(**kwargs):
            kwargs["timeout_seconds"] = 2
            return await real_start(**kwargs)

        client = Client(mcp_server)

        async with client:
            with patch(
                "openfilter_mcp.server.start_approval_server",
                side_effect=short_timeout_start,
            ):
                pending = await client.call_tool(
                    "request_scoped_token",
                    {"scopes": "project:read"},
                )

            assert pending.data["status"] == "pending_approval"
            request_id = pending.data["request_id"]

            # await_token_approval blocks until timeout (2s)
            result = await client.call_tool(
                "await_token_approval",
                {"request_id": request_id},
                raise_on_error=False,
            )

        assert result.data["status"] == "denied"


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
