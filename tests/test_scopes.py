"""Unit tests for openfilter_mcp.scopes."""

import asyncio
import json
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

    def test_cross_resource_wildcard_requires_admin(self):
        # `*:read` is a cross-resource wildcard request — a concrete set of
        # per-resource reads must NOT compose up to it, only `*:*` covers it.
        # Pins the contract called out in server._SERVER_INSTRUCTIONS: there
        # is no `*:read` shorthand in the live /rbac/scopes set.
        assert is_scope_granted("*:read", {"*:*"})
        assert not is_scope_granted("*:read", {"project:read", "filterpipeline:read"})

    def test_malformed_request_always_false(self):
        assert not is_scope_granted("no_colon", {"*:*"})
        assert not is_scope_granted(":action", {"*:*"})
        assert not is_scope_granted("resource:", {"*:*"})
        assert not is_scope_granted("", {"*:*"})

    def test_extra_colon_in_action_rejected_by_wildcards(self):
        # A stray-colon scope is malformed and must be rejected regardless
        # of how broad the grant is — neither a resource-wildcard nor the
        # admin wildcard should paper over a bad shape.
        assert not is_scope_granted("filterpipeline:read:extra", {"filterpipeline:*"})
        assert not is_scope_granted("filterpipeline:read:extra", {"*:*"})
        assert not is_scope_granted("filterpipeline:read:extra", {"filterpipeline:read:extra"})
        # Multiple extra colons: still malformed.
        assert not is_scope_granted("a:b:c:d", {"*:*"})

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

    async def test_malformed_scope_entry_skipped_with_warning(self, httpx_mock, caplog):
        # Per-entry malformation is log-and-skip, not fatal — a single bad
        # row upstream shouldn't disable scope validation wholesale. Only
        # structural failures (missing top-level 'scopes' key, HTTP error,
        # non-JSON body) should raise ScopesUnavailable.
        httpx_mock.add_response(
            url=f"{_BASE_URL}/rbac/scopes",
            json={
                "scopes": [
                    {"value": "project:read", "domain": "project", "action": "read"},
                    {"domain": "broken", "action": "read"},  # missing 'value'
                ],
            },
        )
        with caplog.at_level("WARNING", logger="openfilter_mcp.scopes"):
            async with httpx.AsyncClient(base_url=_BASE_URL) as client:
                result = await fetch_grantable_scopes(client)
        assert result == ["project:read"]
        assert any("malformed" in rec.message.lower() for rec in caplog.records), (
            f"expected a 'malformed' warning, got {[r.message for r in caplog.records]}"
        )

    async def test_all_entries_malformed_yields_empty_list(self, httpx_mock, caplog):
        # Edge case: every entry is bad. We still don't raise — the
        # structural contract (top-level 'scopes' list) was satisfied.
        httpx_mock.add_response(
            url=f"{_BASE_URL}/rbac/scopes",
            json={"scopes": [{"domain": "x"}, "not-a-dict", {"value": 42}]},
        )
        with caplog.at_level("WARNING", logger="openfilter_mcp.scopes"):
            async with httpx.AsyncClient(base_url=_BASE_URL) as client:
                result = await fetch_grantable_scopes(client)
        assert result == []


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
        # The scopes list must be written back to the session cache. (The
        # per-session lock is also written on cold sessions, so we check
        # for this specific call rather than asserting a single write.)
        ctx.set_state.assert_any_await(GRANTABLE_SCOPES_KEY, ["project:read"])

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
        # The lock may be persisted on a cold session, but the scopes list
        # itself must never be written on failure — otherwise a transient
        # error would poison the cache for the rest of the session.
        scope_writes = [
            c for c in ctx.set_state.await_args_list
            if c.args and c.args[0] == GRANTABLE_SCOPES_KEY
        ]
        assert scope_writes == []

    async def test_concurrent_first_fetch_is_deduped(self, httpx_mock):
        """Two concurrent cold-cache callers must share a single
        GET /rbac/scopes via the request-scoped lock + double-check."""
        # Shared per-request state: get_state/set_state read/write this dict
        # so both coroutines see each other's writes like the real ctx would.
        state: dict[str, object] = {}

        async def get_state(key):
            return state.get(key)

        async def set_state(key, value, *, serializable: bool = True):
            # Mirror fastmcp's Context.set_state contract: when `serializable`
            # is True (the default), the value is routed through a pydantic
            # StateValue into key_value storage, which raises on
            # non-json-round-trippable objects (e.g. asyncio.Lock). The real
            # code catches the underlying SerializationError/ValueError and
            # re-raises TypeError. We reproduce that here by round-tripping
            # through json.dumps so tests catch regressions where a caller
            # forgets to pass serializable=False for a non-serializable value.
            if serializable:
                try:
                    json.dumps(value)
                except TypeError as e:
                    raise TypeError(
                        f"Value for state key {key!r} is not serializable. "
                        f"Use set_state({key!r}, value, serializable=False) to "
                        f"store non-serializable values."
                    ) from e
            state[key] = value

        ctx = MagicMock()
        ctx.get_state = AsyncMock(side_effect=get_state)
        ctx.set_state = AsyncMock(side_effect=set_state)

        # Register the endpoint once; pytest-httpx will replay it if called
        # twice, which lets us assert on exact request count.
        httpx_mock.add_response(
            url=f"{_BASE_URL}/rbac/scopes",
            json={"scopes": [{"value": "project:read", "domain": "project", "action": "read"}]},
            is_reusable=True,
        )

        async with httpx.AsyncClient(base_url=_BASE_URL) as client:
            results = await asyncio.gather(
                get_or_fetch_grantable(ctx, client),
                get_or_fetch_grantable(ctx, client),
            )

        assert results == [{"project:read"}, {"project:read"}]
        # Exactly one network call despite two racing callers.
        assert len(httpx_mock.get_requests(url=f"{_BASE_URL}/rbac/scopes")) == 1
