"""Shared test configuration."""

from unittest.mock import patch

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
