"""Unit tests for openfilter_mcp.scopes."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from openfilter_mcp.scopes import (
    GRANTABLE_SCOPES_KEY,
    ScopesUnavailable,
    fetch_grantable_scopes,
    get_or_fetch_grantable,
    is_scope_granted,
    suggest_grantable,
)


# ---------------------------------------------------------------------------
# is_scope_granted
# ---------------------------------------------------------------------------


class TestIsScopeGranted:
    def test_exact_concrete_match(self):
        assert is_scope_granted("filterpipeline:read", {"filterpipeline:read"})

    def test_resource_wildcard_covers_concrete(self):
        assert is_scope_granted("filterpipeline:read", {"filterpipeline:*"})

    def test_admin_wildcard_covers_concrete(self):
        assert is_scope_granted("filterpipeline:read", {"*:*"})

    def test_admin_wildcard_covers_resource_wildcard(self):
        assert is_scope_granted("filterpipeline:*", {"*:*"})

    def test_concrete_miss(self):
        assert not is_scope_granted(
            "filterpipeline:delete",
            {"filterpipeline:read", "filterpipeline:create"},
        )

    def test_other_resource_wildcard_does_not_cover(self):
        assert not is_scope_granted("project:read", {"filterpipeline:*"})

    def test_resource_wildcard_request_against_concrete_set_fails(self):
        # Non-admin can't upgrade concrete tuples to a wildcard.
        assert not is_scope_granted(
            "filterpipeline:*",
            {"filterpipeline:read", "filterpipeline:create", "filterpipeline:update"},
        )

    def test_admin_request_requires_admin_grant(self):
        assert is_scope_granted("*:*", {"*:*"})
        assert not is_scope_granted("*:*", {"filterpipeline:*", "project:*"})

    def test_malformed_request_always_false(self):
        assert not is_scope_granted("no_colon", {"*:*"})
        assert not is_scope_granted(":action", {"*:*"})
        assert not is_scope_granted("resource:", {"*:*"})
        assert not is_scope_granted("", {"*:*"})

    def test_sub_action_scopes(self):
        # The drift bug this ticket fixes: sub-actions like 'start', 'trigger',
        # 'upload' must be accepted when present in the grantable set.
        assert is_scope_granted(
            "pipelineinstance:start",
            {"pipelineinstance:start", "pipelineinstance:stop"},
        )
        assert is_scope_granted("groundtruth:trigger", {"groundtruth:trigger"})
        assert is_scope_granted("groundtruth:upload", {"groundtruth:*"})


# ---------------------------------------------------------------------------
# suggest_grantable
# ---------------------------------------------------------------------------


class TestSuggestGrantable:
    def test_typo_in_action(self):
        grantable = {"filterpipeline:read", "filterpipeline:create"}
        assert suggest_grantable("filterpipeline:raed", grantable) == "filterpipeline:read"

    def test_typo_in_resource(self):
        grantable = {"filterpipeline:read"}
        # Close enough to filterpipeline; difflib should catch it.
        assert suggest_grantable("filterpipelien:read", grantable) == "filterpipeline:read"

    def test_no_reasonable_match(self):
        assert suggest_grantable("zzzz:qqqq", {"filterpipeline:read"}) is None

    def test_empty_grantable(self):
        assert suggest_grantable("project:read", set()) is None


# ---------------------------------------------------------------------------
# fetch_grantable_scopes
# ---------------------------------------------------------------------------


_BASE_URL = "https://api.example"


@pytest.mark.asyncio
class TestFetchGrantableScopes:
    async def test_happy_path(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{_BASE_URL}/rbac/scopes",
            json={
                "scopes": [
                    {"value": "filterpipeline:read", "domain": "filterpipeline", "action": "read"},
                    {"value": "project:*", "domain": "project", "action": "*"},
                ],
            },
        )
        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            result = await fetch_grantable_scopes(client)
        assert result == ["filterpipeline:read", "project:*"]

    async def test_401_raises_with_status(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{_BASE_URL}/rbac/scopes",
            status_code=401,
            json={"error": "unauthenticated"},
        )
        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            with pytest.raises(ScopesUnavailable) as exc_info:
                await fetch_grantable_scopes(client)
        assert exc_info.value.status == 401

    async def test_500_raises_with_status(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{_BASE_URL}/rbac/scopes",
            status_code=500,
            text="internal server error",
        )
        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            with pytest.raises(ScopesUnavailable) as exc_info:
                await fetch_grantable_scopes(client)
        assert exc_info.value.status == 500
        assert "internal server error" in exc_info.value.detail

    async def test_transport_error_raises_with_null_status(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("boom"))
        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            with pytest.raises(ScopesUnavailable) as exc_info:
                await fetch_grantable_scopes(client)
        assert exc_info.value.status is None
        assert "transport error" in exc_info.value.detail

    async def test_missing_scopes_key_raises(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{_BASE_URL}/rbac/scopes",
            json={"not_scopes": []},
        )
        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            with pytest.raises(ScopesUnavailable):
                await fetch_grantable_scopes(client)

    async def test_malformed_scope_entry_raises(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{_BASE_URL}/rbac/scopes",
            json={"scopes": [{"domain": "project", "action": "read"}]},  # no 'value'
        )
        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            with pytest.raises(ScopesUnavailable):
                await fetch_grantable_scopes(client)


# ---------------------------------------------------------------------------
# get_or_fetch_grantable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetOrFetchGrantable:
    async def test_fetches_on_cache_miss_and_persists(self, httpx_mock):
        ctx = MagicMock()
        ctx.get_state = AsyncMock(return_value=None)
        ctx.set_state = AsyncMock()
        httpx_mock.add_response(
            url=f"{_BASE_URL}/rbac/scopes",
            json={"scopes": [{"value": "project:read", "domain": "project", "action": "read"}]},
        )
        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            result = await get_or_fetch_grantable(ctx, client)
        assert result == {"project:read"}
        ctx.set_state.assert_awaited_once_with(GRANTABLE_SCOPES_KEY, ["project:read"])

    async def test_uses_cache_skips_fetch(self, httpx_mock):
        ctx = MagicMock()
        ctx.get_state = AsyncMock(return_value=["cached:scope"])
        ctx.set_state = AsyncMock()
        # No httpx_mock.add_response — pytest-httpx errors if a request is made.
        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            result = await get_or_fetch_grantable(ctx, client)
        assert result == {"cached:scope"}
        ctx.set_state.assert_not_called()

    async def test_failure_not_cached(self, httpx_mock):
        ctx = MagicMock()
        ctx.get_state = AsyncMock(return_value=None)
        ctx.set_state = AsyncMock()
        httpx_mock.add_response(url=f"{_BASE_URL}/rbac/scopes", status_code=500)
        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            with pytest.raises(ScopesUnavailable):
                await get_or_fetch_grantable(ctx, client)
        ctx.set_state.assert_not_called()
