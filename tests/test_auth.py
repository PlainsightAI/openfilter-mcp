"""Tests for the authentication module."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from openfilter_mcp.auth import (
    DEFAULT_API_URL,
    PLAINSIGHT_API_URL,
    AuthenticationError,
    _refresh_token,
    _reset_token_cache,
    _save_token_data,
    create_token_verifier,
    decode_jwt_payload,
    get_api_client,
    get_api_url,
    get_async_api_client,
    get_auth_token,
    get_org_id_from_token,
    get_psctl_token_path,
    read_psctl_token,
)


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset the token cache before and after each test."""
    _reset_token_cache()
    yield
    _reset_token_cache()


class TestCreateTokenVerifier:
    """Tests for create_token_verifier function."""

    def test_creates_debug_token_verifier(self):
        """Should create a DebugTokenVerifier instance."""
        from fastmcp.server.auth.providers.debug import DebugTokenVerifier

        verifier = create_token_verifier()
        assert isinstance(verifier, DebugTokenVerifier)

    @pytest.mark.asyncio
    async def test_verifier_accepts_non_empty_token(self):
        """Should accept any non-empty token."""
        verifier = create_token_verifier()
        # Access the validate function
        result = await verifier.validate("some-token-value")
        assert result is True

    @pytest.mark.asyncio
    async def test_verifier_accepts_jwt_token(self):
        """Should accept JWT tokens."""
        verifier = create_token_verifier()
        jwt_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = await verifier.validate(jwt_token)
        assert result is True

    @pytest.mark.asyncio
    async def test_verifier_accepts_api_token(self):
        """Should accept API tokens (ps_ prefix)."""
        verifier = create_token_verifier()
        api_token = "ps_1234567890abcdef"
        result = await verifier.validate(api_token)
        assert result is True

    @pytest.mark.asyncio
    async def test_verifier_rejects_empty_token(self):
        """Should reject empty tokens."""
        verifier = create_token_verifier()
        result = await verifier.validate("")
        assert result is False

    @pytest.mark.asyncio
    async def test_verifier_rejects_none_token(self):
        """Should reject None tokens."""
        verifier = create_token_verifier()
        result = await verifier.validate(None)
        assert result is False


class TestGetPsctlTokenPath:
    """Tests for get_psctl_token_path function."""

    def test_default_path(self):
        """Should return default XDG path when XDG_CONFIG_HOME not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Clear XDG_CONFIG_HOME if set
            os.environ.pop("XDG_CONFIG_HOME", None)
            path = get_psctl_token_path()
            assert path == Path.home() / ".config" / "plainsight" / "token"

    def test_xdg_config_home_override(self):
        """Should use XDG_CONFIG_HOME when set."""
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom/config"}):
            path = get_psctl_token_path()
            assert path == Path("/custom/config/plainsight/token")


class TestReadPsctlToken:
    """Tests for read_psctl_token function."""

    def test_returns_none_when_file_not_exists(self, tmp_path):
        """Should return None when token file doesn't exist."""
        with patch(
            "openfilter_mcp.auth.get_psctl_token_path",
            return_value=tmp_path / "nonexistent" / "token",
        ):
            token = read_psctl_token()
            assert token is None

    def test_returns_access_token_from_file(self, tmp_path):
        """Should return access_token from valid token file."""
        token_file = tmp_path / "token"
        token_data = {
            "access_token": "psctl-test-token-12345",
            "refresh_token": "refresh-token",
            "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        token_file.write_text(json.dumps(token_data))

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            token = read_psctl_token()
            assert token == "psctl-test-token-12345"

    def test_returns_none_for_expired_token(self, tmp_path):
        """Should return None when token is expired."""
        token_file = tmp_path / "token"
        token_data = {
            "access_token": "expired-token",
            "expiry": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        }
        token_file.write_text(json.dumps(token_data))

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            token = read_psctl_token()
            assert token is None

    def test_returns_token_without_expiry(self, tmp_path):
        """Should return token when expiry field is missing."""
        token_file = tmp_path / "token"
        token_data = {"access_token": "no-expiry-token"}
        token_file.write_text(json.dumps(token_data))

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            token = read_psctl_token()
            assert token == "no-expiry-token"

    def test_returns_none_for_invalid_json(self, tmp_path):
        """Should return None when token file contains invalid JSON."""
        token_file = tmp_path / "token"
        token_file.write_text("not valid json {")

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            token = read_psctl_token()
            assert token is None

    def test_returns_none_when_access_token_missing(self, tmp_path):
        """Should return None when access_token field is missing."""
        token_file = tmp_path / "token"
        token_data = {"refresh_token": "only-refresh"}
        token_file.write_text(json.dumps(token_data))

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            token = read_psctl_token()
            assert token is None

    def test_handles_z_suffix_in_expiry(self, tmp_path):
        """Should handle Z suffix in ISO datetime expiry."""
        token_file = tmp_path / "token"
        future_time = datetime.now(timezone.utc) + timedelta(hours=1)
        token_data = {
            "access_token": "z-suffix-token",
            "expiry": future_time.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        token_file.write_text(json.dumps(token_data))

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            token = read_psctl_token()
            assert token == "z-suffix-token"


class TestGetAuthToken:
    """Tests for get_auth_token function."""

    def test_returns_none_when_no_psctl_token(self):
        """Should return None when no psctl token available."""
        with patch("openfilter_mcp.auth.read_psctl_token", return_value=None):
            token = get_auth_token()
            assert token is None

    def test_returns_psctl_token(self):
        """Should return token from psctl config."""
        with patch(
            "openfilter_mcp.auth.read_psctl_token", return_value="psctl-token"
        ):
            token = get_auth_token()
            assert token == "psctl-token"


class TestGetApiClient:
    """Tests for get_api_client function."""

    def test_raises_authentication_error_without_token(self):
        """Should raise AuthenticationError when no token is available."""
        with patch("openfilter_mcp.auth.get_auth_token", return_value=None):
            with pytest.raises(AuthenticationError) as exc_info:
                get_api_client()
            assert "No authentication token available" in str(exc_info.value)

    def test_creates_client_with_authorization_header(self):
        """Should create httpx.Client with Authorization header."""
        with patch("openfilter_mcp.auth.get_auth_token", return_value="test-token"):
            client = get_api_client()
            try:
                assert isinstance(client, httpx.Client)
                assert client.headers["Authorization"] == "Bearer test-token"
                # Base URL includes trailing slash
                assert str(client.base_url).rstrip("/") == get_api_url()
            finally:
                client.close()

    def test_uses_custom_timeout(self):
        """Should use custom timeout when specified."""
        with patch("openfilter_mcp.auth.get_auth_token", return_value="test-token"):
            client = get_api_client(timeout=60.0)
            try:
                assert client.timeout.connect == 60.0
            finally:
                client.close()


class TestGetAsyncApiClient:
    """Tests for get_async_api_client function."""

    def test_raises_authentication_error_without_token(self):
        """Should raise AuthenticationError when no token is available."""
        with patch("openfilter_mcp.auth.get_auth_token", return_value=None):
            with pytest.raises(AuthenticationError) as exc_info:
                get_async_api_client()
            assert "No authentication token available" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_creates_async_client_with_authorization_header(self):
        """Should create httpx.AsyncClient with Authorization header."""
        with patch("openfilter_mcp.auth.get_auth_token", return_value="test-token"):
            client = get_async_api_client()
            try:
                assert isinstance(client, httpx.AsyncClient)
                assert client.headers["Authorization"] == "Bearer test-token"
                # Base URL includes trailing slash
                assert str(client.base_url).rstrip("/") == get_api_url()
            finally:
                await client.aclose()


# Sample JWT for testing - org_id in both app_metadata and user_metadata
# Note: This is a test token with an invalid signature. The signature verification
# is not performed by this module (it's handled by plainsight-api), so we only need
# a valid base64-encoded header and payload for testing the decoding logic.
SAMPLE_JWT_WITH_ORG = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJhcHBfbWV0YWRhdGEiOnsib3JnYW5pemF0aW9uX2lkIjoiNDhlZWMxN2QtMzA4OS00ZDEzLWExMDctMjRmNWY0Y2Y4NGM3In0sInVzZXJfbWV0YWRhdGEiOnsib3JnYW5pemF0aW9uX2lkIjoiNDhlZWMxN2QtMzA4OS00ZDEzLWExMDctMjRmNWY0Y2Y4NGM3In19."
    "signature"
)

# Sample JWT without organization_id
SAMPLE_JWT_NO_ORG = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJhcHBfbWV0YWRhdGEiOnt9LCJ1c2VyX21ldGFkYXRhIjp7fX0."
    "signature"
)


class TestDecodeJwtPayload:
    """Tests for decode_jwt_payload function."""

    def test_decodes_valid_jwt(self):
        """Should decode payload from valid JWT."""
        payload = decode_jwt_payload(SAMPLE_JWT_WITH_ORG)
        assert payload is not None
        assert "app_metadata" in payload
        assert payload["app_metadata"]["organization_id"] == "48eec17d-3089-4d13-a107-24f5f4cf84c7"

    def test_returns_none_for_invalid_jwt(self):
        """Should return None for invalid JWT format."""
        assert decode_jwt_payload("not-a-jwt") is None
        assert decode_jwt_payload("only.two") is None
        assert decode_jwt_payload("") is None

    def test_handles_padding_correctly(self):
        """Should handle base64 padding correctly."""
        # This JWT payload decodes to {"test": "value"}
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0IjoidmFsdWUifQ.sig"
        payload = decode_jwt_payload(jwt)
        assert payload is not None
        assert payload.get("test") == "value"


class TestGetOrgIdFromToken:
    """Tests for get_org_id_from_token function."""

    def test_extracts_org_id_from_app_metadata(self):
        """Should extract org_id from app_metadata."""
        org_id = get_org_id_from_token(SAMPLE_JWT_WITH_ORG)
        assert org_id == "48eec17d-3089-4d13-a107-24f5f4cf84c7"

    def test_returns_none_when_no_org_id(self):
        """Should return None when no organization_id in token."""
        org_id = get_org_id_from_token(SAMPLE_JWT_NO_ORG)
        assert org_id is None

    def test_returns_none_for_invalid_token(self):
        """Should return None for invalid token."""
        assert get_org_id_from_token("invalid") is None
        assert get_org_id_from_token("") is None

    def test_returns_none_for_api_token(self):
        """Should return None for API tokens (ps_ prefix)."""
        # API tokens don't have JWT structure
        assert get_org_id_from_token("ps_abc123") is None


class TestApiClientWithOrgHeader:
    """Tests for X-Scope-OrgID header in API clients."""

    def test_sync_client_includes_org_header(self):
        """Should include X-Scope-OrgID header when token has org_id."""
        with patch("openfilter_mcp.auth.get_auth_token", return_value=SAMPLE_JWT_WITH_ORG):
            client = get_api_client()
            try:
                assert client.headers["X-Scope-OrgID"] == "48eec17d-3089-4d13-a107-24f5f4cf84c7"
            finally:
                client.close()

    def test_sync_client_no_org_header_when_missing(self):
        """Should not include X-Scope-OrgID header when token lacks org_id."""
        with patch("openfilter_mcp.auth.get_auth_token", return_value="simple-token"):
            client = get_api_client()
            try:
                assert "X-Scope-OrgID" not in client.headers
            finally:
                client.close()

    @pytest.mark.asyncio
    async def test_async_client_includes_org_header(self):
        """Should include X-Scope-OrgID header in async client when token has org_id."""
        with patch("openfilter_mcp.auth.get_auth_token", return_value=SAMPLE_JWT_WITH_ORG):
            client = get_async_api_client()
            try:
                assert client.headers["X-Scope-OrgID"] == "48eec17d-3089-4d13-a107-24f5f4cf84c7"
            finally:
                await client.aclose()

    @pytest.mark.asyncio
    async def test_async_client_no_org_header_when_missing(self):
        """Should not include X-Scope-OrgID header in async client when token lacks org_id."""
        with patch("openfilter_mcp.auth.get_auth_token", return_value="simple-token"):
            client = get_async_api_client()
            try:
                assert "X-Scope-OrgID" not in client.headers
            finally:
                await client.aclose()


class TestApiUrlConfiguration:
    """Tests for API URL configuration with psctl-compliant env vars."""

    def test_default_url_constant(self):
        """Should have correct default URL constant."""
        assert DEFAULT_API_URL == "https://api.prod.plainsight.tech"

    def test_plainsight_api_url_backwards_compat(self):
        """Should expose PLAINSIGHT_API_URL for backwards compatibility."""
        # PLAINSIGHT_API_URL is evaluated at import time
        assert PLAINSIGHT_API_URL == DEFAULT_API_URL

    def test_get_api_url_returns_default_when_no_env_vars(self):
        """Should return default URL when no env vars are set."""
        with patch.dict(os.environ, {}, clear=True):
            # Clear any existing env vars
            os.environ.pop("PS_API_URL", None)
            os.environ.pop("PSCTL_API_URL", None)
            os.environ.pop("PLAINSIGHT_API_URL", None)
            assert get_api_url() == DEFAULT_API_URL

    def test_ps_api_url_takes_highest_precedence(self):
        """PS_API_URL should take highest precedence (matches psctl CLI)."""
        ps_url = "https://ps.api.example.com"
        psctl_url = "https://psctl.api.example.com"
        plainsight_url = "https://plainsight.api.example.com"
        with patch.dict(
            os.environ,
            {
                "PS_API_URL": ps_url,
                "PSCTL_API_URL": psctl_url,
                "PLAINSIGHT_API_URL": plainsight_url,
            },
        ):
            assert get_api_url() == ps_url

    def test_psctl_api_url_takes_precedence_over_plainsight(self):
        """PSCTL_API_URL should take precedence over PLAINSIGHT_API_URL."""
        psctl_url = "https://psctl.api.example.com"
        plainsight_url = "https://plainsight.api.example.com"
        with patch.dict(
            os.environ,
            {"PSCTL_API_URL": psctl_url, "PLAINSIGHT_API_URL": plainsight_url},
        ):
            os.environ.pop("PS_API_URL", None)
            assert get_api_url() == psctl_url

    def test_plainsight_api_url_fallback(self):
        """Should fall back to PLAINSIGHT_API_URL when others not set."""
        plainsight_url = "https://plainsight.api.example.com"
        with patch.dict(os.environ, {"PLAINSIGHT_API_URL": plainsight_url}):
            # Clear higher-priority env vars
            os.environ.pop("PS_API_URL", None)
            os.environ.pop("PSCTL_API_URL", None)
            assert get_api_url() == plainsight_url

    def test_ps_api_url_only(self):
        """Should use PS_API_URL when only it is set."""
        ps_url = "https://ps.api.example.com"
        with patch.dict(os.environ, {"PS_API_URL": ps_url}):
            os.environ.pop("PSCTL_API_URL", None)
            os.environ.pop("PLAINSIGHT_API_URL", None)
            assert get_api_url() == ps_url

    def test_psctl_api_url_only(self):
        """Should use PSCTL_API_URL when only it is set."""
        psctl_url = "https://psctl.api.example.com"
        with patch.dict(os.environ, {"PSCTL_API_URL": psctl_url}):
            os.environ.pop("PS_API_URL", None)
            os.environ.pop("PLAINSIGHT_API_URL", None)
            assert get_api_url() == psctl_url

    def test_empty_ps_api_url_falls_through(self):
        """Empty PS_API_URL should fall through to PSCTL_API_URL."""
        psctl_url = "https://psctl.api.example.com"
        with patch.dict(
            os.environ, {"PS_API_URL": "", "PSCTL_API_URL": psctl_url}
        ):
            os.environ.pop("PLAINSIGHT_API_URL", None)
            assert get_api_url() == psctl_url

    def test_empty_psctl_api_url_falls_through(self):
        """Empty PSCTL_API_URL should fall through to PLAINSIGHT_API_URL."""
        plainsight_url = "https://plainsight.api.example.com"
        with patch.dict(
            os.environ, {"PSCTL_API_URL": "", "PLAINSIGHT_API_URL": plainsight_url}
        ):
            os.environ.pop("PS_API_URL", None)
            assert get_api_url() == plainsight_url

    def test_module_reload_with_ps_api_url(self):
        """Should use PS_API_URL after module reload."""
        custom_url = "https://custom.ps.api.example.com"
        with patch.dict(os.environ, {"PS_API_URL": custom_url}):
            os.environ.pop("PSCTL_API_URL", None)
            os.environ.pop("PLAINSIGHT_API_URL", None)
            import importlib

            import openfilter_mcp.auth as auth_module

            importlib.reload(auth_module)
            assert auth_module.PLAINSIGHT_API_URL == custom_url
            assert auth_module.get_api_url() == custom_url
            # Restore original
            os.environ.pop("PS_API_URL", None)
            importlib.reload(auth_module)


class TestRefreshToken:
    """Tests for _refresh_token function."""

    def test_extracts_token_from_nested_response(self):
        """Should extract inner token from API response with 'token' wrapper."""
        # API returns {"token": {...}} structure
        api_response = {
            "token": {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expiry": "2025-12-24T00:00:00+00:00",
            }
        }

        mock_response = httpx.Response(200, json=api_response)
        with patch("openfilter_mcp.auth.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            result = _refresh_token("old-refresh-token")

        assert result is not None
        assert result["access_token"] == "new-access-token"
        assert result["refresh_token"] == "new-refresh-token"
        # Should return flat structure, not nested
        assert "token" not in result

    def test_handles_flat_response(self):
        """Should handle API response without 'token' wrapper (backwards compat)."""
        api_response = {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expiry": "2025-12-24T00:00:00+00:00",
        }

        mock_response = httpx.Response(200, json=api_response)
        with patch("openfilter_mcp.auth.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            result = _refresh_token("old-refresh-token")

        assert result is not None
        assert result["access_token"] == "new-access-token"

    def test_returns_none_on_error_response(self):
        """Should return None when API returns error status."""
        mock_response = httpx.Response(401, json={"error": "Invalid refresh token"})
        with patch("openfilter_mcp.auth.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            result = _refresh_token("invalid-refresh-token")

        assert result is None

    def test_returns_none_on_network_error(self):
        """Should return None when network error occurs."""
        with patch("openfilter_mcp.auth.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.side_effect = httpx.ConnectError("Connection failed")

            result = _refresh_token("some-refresh-token")

        assert result is None

    def test_sends_bearer_authorization_header(self):
        """Should send refresh token as Bearer token in Authorization header."""
        mock_response = httpx.Response(200, json={"token": {"access_token": "new-token"}})
        with patch("openfilter_mcp.auth.httpx.Client") as mock_client:
            mock_client_instance = mock_client.return_value.__enter__.return_value
            mock_client_instance.post.return_value = mock_response

            _refresh_token("my-refresh-token")

            mock_client_instance.post.assert_called_once_with(
                "/auth/token/refresh",
                headers={"Authorization": "Bearer my-refresh-token"},
            )


class TestSaveTokenData:
    """Tests for _save_token_data function."""

    def test_saves_token_to_file(self, tmp_path):
        """Should save token data to the correct file."""
        token_file = tmp_path / "token"
        token_data = {
            "access_token": "test-access-token",
            "refresh_token": "test-refresh-token",
            "expiry": "2025-12-24T00:00:00+00:00",
        }

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            result = _save_token_data(token_data)

        assert result is True
        assert token_file.exists()
        saved_data = json.loads(token_file.read_text())
        assert saved_data == token_data

    def test_creates_parent_directories(self, tmp_path):
        """Should create parent directories if they don't exist."""
        token_file = tmp_path / "subdir" / "nested" / "token"
        token_data = {"access_token": "test-token"}

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            result = _save_token_data(token_data)

        assert result is True
        assert token_file.exists()

    def test_sets_secure_permissions(self, tmp_path):
        """Should set file permissions to 0600 (owner read/write only)."""
        token_file = tmp_path / "token"
        token_data = {"access_token": "test-token"}

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            _save_token_data(token_data)

        # Check file permissions (0600 = owner read/write only)
        file_mode = token_file.stat().st_mode & 0o777
        assert file_mode == 0o600

    def test_returns_false_on_permission_error(self, tmp_path):
        """Should return False when unable to write file."""
        # Use a path that will fail (e.g., root directory on Unix)
        token_file = Path("/root/cannot_write_here/token")

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            result = _save_token_data({"access_token": "test"})

        assert result is False


class TestTokenRefreshFlow:
    """Integration tests for the complete token refresh flow."""

    def test_refreshes_expired_token_and_saves(self, tmp_path):
        """Should refresh expired token and save the new token to disk."""
        token_file = tmp_path / "token"
        # Create an expired token with refresh token
        expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
        old_token_data = {
            "access_token": "old-expired-token",
            "refresh_token": "valid-refresh-token",
            "expiry": expired_time.isoformat(),
        }
        token_file.write_text(json.dumps(old_token_data))

        # Mock the refresh response
        new_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        api_response = {
            "token": {
                "access_token": "new-fresh-token",
                "refresh_token": "new-refresh-token",
                "expiry": new_expiry.isoformat(),
            }
        }
        mock_response = httpx.Response(200, json=api_response)

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            with patch("openfilter_mcp.auth.httpx.Client") as mock_client:
                mock_client.return_value.__enter__.return_value.post.return_value = mock_response
                token = read_psctl_token()

        # Should return the new token
        assert token == "new-fresh-token"

        # Should have saved the new token to disk
        saved_data = json.loads(token_file.read_text())
        assert saved_data["access_token"] == "new-fresh-token"
        assert saved_data["refresh_token"] == "new-refresh-token"
        # Should NOT have the wrapper
        assert "token" not in saved_data

    def test_refreshes_token_expiring_within_5_minutes(self, tmp_path):
        """Should refresh token when it will expire within 5 minutes."""
        token_file = tmp_path / "token"
        # Create a token expiring in 3 minutes (within 5-minute threshold)
        almost_expired_time = datetime.now(timezone.utc) + timedelta(minutes=3)
        old_token_data = {
            "access_token": "almost-expired-token",
            "refresh_token": "valid-refresh-token",
            "expiry": almost_expired_time.isoformat(),
        }
        token_file.write_text(json.dumps(old_token_data))

        # Mock the refresh response
        new_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        api_response = {
            "token": {
                "access_token": "refreshed-token",
                "refresh_token": "new-refresh-token",
                "expiry": new_expiry.isoformat(),
            }
        }
        mock_response = httpx.Response(200, json=api_response)

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            with patch("openfilter_mcp.auth.httpx.Client") as mock_client:
                mock_client.return_value.__enter__.return_value.post.return_value = mock_response
                token = read_psctl_token()

        assert token == "refreshed-token"

    def test_returns_none_when_refresh_fails(self, tmp_path):
        """Should return None when token is expired and refresh fails."""
        token_file = tmp_path / "token"
        expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
        old_token_data = {
            "access_token": "expired-token",
            "refresh_token": "invalid-refresh-token",
            "expiry": expired_time.isoformat(),
        }
        token_file.write_text(json.dumps(old_token_data))

        # Mock refresh failure
        mock_response = httpx.Response(401, json={"error": "Invalid refresh token"})

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            with patch("openfilter_mcp.auth.httpx.Client") as mock_client:
                mock_client.return_value.__enter__.return_value.post.return_value = mock_response
                token = read_psctl_token()

        assert token is None

    def test_returns_none_when_no_refresh_token(self, tmp_path):
        """Should return None when token is expired and no refresh token available."""
        token_file = tmp_path / "token"
        expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
        old_token_data = {
            "access_token": "expired-token",
            # No refresh_token field
            "expiry": expired_time.isoformat(),
        }
        token_file.write_text(json.dumps(old_token_data))

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            token = read_psctl_token()

        assert token is None

    def test_handles_nested_token_structure_from_psctl(self, tmp_path):
        """Should handle token file with nested 'token' wrapper from psctl login."""
        token_file = tmp_path / "token"
        expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
        # psctl might save with a wrapper structure
        nested_token_data = {
            "token": {
                "access_token": "old-token",
                "refresh_token": "valid-refresh-token",
                "expiry": expired_time.isoformat(),
            }
        }
        token_file.write_text(json.dumps(nested_token_data))

        # Mock the refresh response
        new_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        api_response = {
            "token": {
                "access_token": "new-token",
                "refresh_token": "new-refresh-token",
                "expiry": new_expiry.isoformat(),
            }
        }
        mock_response = httpx.Response(200, json=api_response)

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            with patch("openfilter_mcp.auth.httpx.Client") as mock_client:
                mock_client.return_value.__enter__.return_value.post.return_value = mock_response
                token = read_psctl_token()

        assert token == "new-token"
