"""Shared test configuration."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def allow_unscoped_token_in_tests():
    """Disable scoped-token enforcement for most tests.

    The default behavior requires a scoped token for all API calls.
    Tests that specifically verify enforcement behavior can override
    this by patching ALLOW_UNSCOPED_TOKEN themselves.
    """
    with patch("openfilter_mcp.entity_tools.ALLOW_UNSCOPED_TOKEN", True):
        yield


@pytest.fixture(autouse=True)
def stub_grantable_scopes_in_server():
    """Short-circuit request_scoped_token's live /rbac/scopes fetch for tests
    that don't specifically exercise it.

    Patches the binding inside openfilter_mcp.server only — tests in
    test_scopes.py import from openfilter_mcp.scopes and are unaffected.
    """
    try:
        with patch(
            "openfilter_mcp.server.get_or_fetch_grantable",
            new=AsyncMock(return_value={"*:*"}),
        ):
            yield
    except (ImportError, AttributeError):
        yield
