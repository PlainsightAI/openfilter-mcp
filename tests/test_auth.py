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
    PLAINSIGHT_EMAIL_DOMAIN,
    AuthenticationError,
    TokenRefreshTransport,
    _async_refresh_token,
    _get_refresh_token_from_file,
    _refresh_token,
    _reset_token_cache,
    _save_token_data,
    create_token_verifier,
    decode_jwt_payload,
    get_api_client,
    get_api_url,
    get_async_api_client,
    get_async_api_client_with_retry,
    get_auth_token,
    get_effective_org_id,
    get_org_id_from_token,
    get_psctl_token_path,
    is_plainsight_employee,
    read_psctl_token,
    refresh_and_get_new_token,
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

    def test_returns_path_in_config_dir(self):
        """Should return path using platformdirs user_config_dir."""
        path = get_psctl_token_path()
        # The path should end with plainsight/token
        assert path.name == "token"
        assert path.parent.name == "plainsight"

    def test_path_uses_platformdirs(self):
        """Should use platformdirs.user_config_dir for the base directory."""
        import platformdirs

        expected_config_dir = platformdirs.user_config_dir("plainsight")
        path = get_psctl_token_path()
        assert str(path) == str(Path(expected_config_dir) / "token")


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

# Sample JWT for Plainsight employee (email ends with @plainsight.ai)
PLAINSIGHT_EMPLOYEE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJlbWFpbCI6ICJzYW1AcGxhaW5zaWdodC5haSIsICJhcHBfbWV0YWRhdGEiOiB7Im9yZ2FuaXphdGlvbl9pZCI6ICJwbGFpbnNpZ2h0LW9yZy1pZCJ9LCAidXNlcl9tZXRhZGF0YSI6IHt9fQ."
    "signature"
)

# Sample JWT for non-employee (external customer)
NON_EMPLOYEE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJlbWFpbCI6ICJjdXN0b21lckBtY2RvbmFsZHMuY29tIiwgImFwcF9tZXRhZGF0YSI6IHsib3JnYW5pemF0aW9uX2lkIjogIm1jZG9uYWxkcy1vcmctaWQifSwgInVzZXJfbWV0YWRhdGEiOiB7fX0."
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


class TestAsyncRefreshToken:
    """Tests for _async_refresh_token function."""

    @pytest.mark.asyncio
    async def test_extracts_token_from_nested_response(self):
        """Should extract inner token from API response with 'token' wrapper."""
        api_response = {
            "token": {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expiry": "2025-12-24T00:00:00+00:00",
            }
        }

        mock_response = httpx.Response(200, json=api_response)
        with patch("openfilter_mcp.auth.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = (
                lambda *args, **kwargs: mock_response
            )
            # Make it async by wrapping
            async def async_post(*args, **kwargs):
                return mock_response
            mock_client.return_value.__aenter__.return_value.post = async_post

            result = await _async_refresh_token("old-refresh-token")

        assert result is not None
        assert result["access_token"] == "new-access-token"
        assert "token" not in result

    @pytest.mark.asyncio
    async def test_returns_none_on_error_response(self):
        """Should return None when API returns error status."""
        mock_response = httpx.Response(401, json={"error": "Invalid"})
        with patch("openfilter_mcp.auth.httpx.AsyncClient") as mock_client:
            async def async_post(*args, **kwargs):
                return mock_response
            mock_client.return_value.__aenter__.return_value.post = async_post

            result = await _async_refresh_token("invalid-token")

        assert result is None


class TestGetRefreshTokenFromFile:
    """Tests for _get_refresh_token_from_file function."""

    def test_returns_refresh_token_from_file(self, tmp_path):
        """Should return refresh_token from valid token file."""
        token_file = tmp_path / "token"
        token_data = {
            "access_token": "access",
            "refresh_token": "my-refresh-token",
        }
        token_file.write_text(json.dumps(token_data))

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            refresh_token = _get_refresh_token_from_file()

        assert refresh_token == "my-refresh-token"

    def test_returns_none_when_file_not_exists(self, tmp_path):
        """Should return None when token file doesn't exist."""
        with patch(
            "openfilter_mcp.auth.get_psctl_token_path",
            return_value=tmp_path / "nonexistent",
        ):
            refresh_token = _get_refresh_token_from_file()

        assert refresh_token is None

    def test_handles_nested_structure(self, tmp_path):
        """Should handle nested token structure from psctl."""
        token_file = tmp_path / "token"
        token_data = {
            "token": {
                "access_token": "access",
                "refresh_token": "nested-refresh-token",
            }
        }
        token_file.write_text(json.dumps(token_data))

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            refresh_token = _get_refresh_token_from_file()

        assert refresh_token == "nested-refresh-token"

    def test_returns_none_when_no_refresh_token(self, tmp_path):
        """Should return None when refresh_token field is missing."""
        token_file = tmp_path / "token"
        token_data = {"access_token": "access-only"}
        token_file.write_text(json.dumps(token_data))

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            refresh_token = _get_refresh_token_from_file()

        assert refresh_token is None


class TestRefreshAndGetNewToken:
    """Tests for refresh_and_get_new_token function."""

    @pytest.mark.asyncio
    async def test_refreshes_and_returns_new_token(self, tmp_path):
        """Should refresh token and return new access token."""
        token_file = tmp_path / "token"
        token_data = {
            "access_token": "old-token",
            "refresh_token": "valid-refresh",
        }
        token_file.write_text(json.dumps(token_data))

        new_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        api_response = {
            "token": {
                "access_token": "brand-new-token",
                "refresh_token": "new-refresh",
                "expiry": new_expiry.isoformat(),
            }
        }
        mock_response = httpx.Response(200, json=api_response)

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            with patch("openfilter_mcp.auth.httpx.AsyncClient") as mock_client:
                async def async_post(*args, **kwargs):
                    return mock_response
                mock_client.return_value.__aenter__.return_value.post = async_post

                new_token = await refresh_and_get_new_token()

        assert new_token == "brand-new-token"
        # Token should be saved
        saved_data = json.loads(token_file.read_text())
        assert saved_data["access_token"] == "brand-new-token"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_refresh_token(self, tmp_path):
        """Should return None when no refresh token available."""
        token_file = tmp_path / "token"
        token_data = {"access_token": "access-only"}
        token_file.write_text(json.dumps(token_data))

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            new_token = await refresh_and_get_new_token()

        assert new_token is None

    @pytest.mark.asyncio
    async def test_returns_none_when_refresh_fails(self, tmp_path):
        """Should return None when token refresh fails."""
        token_file = tmp_path / "token"
        token_data = {
            "access_token": "old",
            "refresh_token": "invalid-refresh",
        }
        token_file.write_text(json.dumps(token_data))

        mock_response = httpx.Response(401, json={"error": "Invalid"})

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            with patch("openfilter_mcp.auth.httpx.AsyncClient") as mock_client:
                async def async_post(*args, **kwargs):
                    return mock_response
                mock_client.return_value.__aenter__.return_value.post = async_post

                new_token = await refresh_and_get_new_token()

        assert new_token is None

    @pytest.mark.asyncio
    async def test_clears_cache_before_refresh(self, tmp_path):
        """Should clear token cache before attempting refresh."""
        import openfilter_mcp.auth as auth_module

        token_file = tmp_path / "token"
        token_data = {
            "access_token": "old",
            "refresh_token": "valid-refresh",
        }
        token_file.write_text(json.dumps(token_data))

        # Set up cached token
        auth_module._cached_token = "cached-old-token"
        auth_module._cached_token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

        new_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        api_response = {
            "token": {
                "access_token": "new-token",
                "refresh_token": "new-refresh",
                "expiry": new_expiry.isoformat(),
            }
        }
        mock_response = httpx.Response(200, json=api_response)

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            with patch("openfilter_mcp.auth.httpx.AsyncClient") as mock_client:
                async def async_post(*args, **kwargs):
                    return mock_response
                mock_client.return_value.__aenter__.return_value.post = async_post

                new_token = await refresh_and_get_new_token()

        assert new_token == "new-token"
        # Cache should be updated with new token
        assert auth_module._cached_token == "new-token"


class TestTokenRefreshTransport:
    """Tests for TokenRefreshTransport class."""

    @pytest.mark.asyncio
    async def test_passes_through_successful_requests(self):
        """Should pass through requests that succeed without 401."""

        class MockTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, json={"success": True})

        transport = TokenRefreshTransport(
            transport=MockTransport(),
            get_org_id=lambda token: None,
        )

        request = httpx.Request("GET", "https://api.example.com/test")
        response = await transport.handle_async_request(request)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_retries_on_401_after_refresh(self, tmp_path):
        """Should retry request with new token after 401 due to expiration."""
        token_file = tmp_path / "token"
        token_data = {
            "access_token": "old",
            "refresh_token": "valid-refresh",
        }
        token_file.write_text(json.dumps(token_data))

        call_count = 0

        class MockTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # First call returns 401 with expiration error
                    return httpx.Response(
                        401,
                        json={
                            "detail": "Invalid token",
                            "errors": [{"message": "token is expired"}],
                        },
                    )
                # Retry succeeds
                return httpx.Response(200, json={"success": True})

        new_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        api_response = {
            "token": {
                "access_token": "new-token",
                "refresh_token": "new-refresh",
                "expiry": new_expiry.isoformat(),
            }
        }
        mock_refresh_response = httpx.Response(200, json=api_response)

        transport = TokenRefreshTransport(
            transport=MockTransport(),
            get_org_id=lambda token: None,
        )

        request = httpx.Request(
            "GET",
            "https://api.example.com/test",
            headers={"Authorization": "Bearer old"},
        )

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            with patch("openfilter_mcp.auth.httpx.AsyncClient") as mock_client:
                async def async_post(*args, **kwargs):
                    return mock_refresh_response
                mock_client.return_value.__aenter__.return_value.post = async_post

                response = await transport.handle_async_request(request)

        assert response.status_code == 200
        assert call_count == 2  # Original + retry

    @pytest.mark.asyncio
    async def test_returns_401_when_refresh_fails(self, tmp_path):
        """Should return 401 when token refresh fails."""
        token_file = tmp_path / "token"
        token_data = {
            "access_token": "old",
            "refresh_token": "invalid-refresh",
        }
        token_file.write_text(json.dumps(token_data))

        class MockTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                # Return 401 with expiration error to trigger refresh attempt
                return httpx.Response(
                    401,
                    json={
                        "detail": "Invalid token",
                        "errors": [{"message": "token is expired"}],
                    },
                )

        mock_refresh_response = httpx.Response(401, json={"error": "Invalid refresh"})

        transport = TokenRefreshTransport(
            transport=MockTransport(),
            get_org_id=lambda token: None,
        )

        request = httpx.Request(
            "GET",
            "https://api.example.com/test",
            headers={"Authorization": "Bearer old"},
        )

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            with patch("openfilter_mcp.auth.httpx.AsyncClient") as mock_client:
                async def async_post(*args, **kwargs):
                    return mock_refresh_response
                mock_client.return_value.__aenter__.return_value.post = async_post

                response = await transport.handle_async_request(request)

        # Should return original 401 since refresh failed
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_updates_org_id_header_on_retry(self, tmp_path):
        """Should update X-Scope-OrgID header when retrying with new token."""
        token_file = tmp_path / "token"
        token_data = {
            "access_token": "old",
            "refresh_token": "valid-refresh",
        }
        token_file.write_text(json.dumps(token_data))

        captured_headers = []

        class MockTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                captured_headers.append(dict(request.headers))
                if len(captured_headers) == 1:
                    # Return 401 with expiration error to trigger refresh
                    return httpx.Response(
                        401,
                        json={
                            "detail": "Invalid token",
                            "errors": [{"message": "token is expired"}],
                        },
                    )
                return httpx.Response(200, json={"success": True})

        new_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        api_response = {
            "token": {
                "access_token": SAMPLE_JWT_WITH_ORG,
                "refresh_token": "new-refresh",
                "expiry": new_expiry.isoformat(),
            }
        }
        mock_refresh_response = httpx.Response(200, json=api_response)

        transport = TokenRefreshTransport(
            transport=MockTransport(),
            get_org_id=get_org_id_from_token,
        )

        request = httpx.Request(
            "GET",
            "https://api.example.com/test",
            headers={"Authorization": "Bearer old"},
        )

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            with patch("openfilter_mcp.auth.httpx.AsyncClient") as mock_client:
                async def async_post(*args, **kwargs):
                    return mock_refresh_response
                mock_client.return_value.__aenter__.return_value.post = async_post

                response = await transport.handle_async_request(request)

        assert response.status_code == 200
        # Second request should have updated token and org ID
        assert len(captured_headers) == 2
        assert f"Bearer {SAMPLE_JWT_WITH_ORG}" in captured_headers[1]["authorization"]
        assert captured_headers[1].get("x-scope-orgid") == "48eec17d-3089-4d13-a107-24f5f4cf84c7"

    @pytest.mark.asyncio
    async def test_does_not_refresh_on_non_expiration_401(self):
        """Should NOT refresh token when 401 is not due to expiration."""
        call_count = 0

        class MockTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal call_count
                call_count += 1
                # Return 401 without expiration error (e.g., invalid token)
                return httpx.Response(
                    401,
                    json={
                        "detail": "Invalid token",
                        "errors": [{"message": "auth: unauthorized to validate token"}],
                    },
                )

        transport = TokenRefreshTransport(
            transport=MockTransport(),
            get_org_id=lambda token: None,
        )

        request = httpx.Request(
            "GET",
            "https://api.example.com/test",
            headers={"Authorization": "Bearer invalid"},
        )

        # Should NOT attempt refresh, so no need to patch refresh functions
        response = await transport.handle_async_request(request)

        assert response.status_code == 401
        assert call_count == 1  # Only original request, no retry

    @pytest.mark.asyncio
    async def test_does_not_refresh_on_revoked_token_401(self):
        """Should NOT refresh token when 401 is due to revoked token."""
        call_count = 0

        class MockTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal call_count
                call_count += 1
                # Return 401 for revoked API token
                return httpx.Response(
                    401,
                    json={
                        "detail": "Invalid API token",
                        "errors": [{"message": "api_token: unauthorized to revoked"}],
                    },
                )

        transport = TokenRefreshTransport(
            transport=MockTransport(),
            get_org_id=lambda token: None,
        )

        request = httpx.Request(
            "GET",
            "https://api.example.com/test",
            headers={"Authorization": "Bearer revoked-token"},
        )

        response = await transport.handle_async_request(request)

        assert response.status_code == 401
        assert call_count == 1  # Only original request, no retry

    @pytest.mark.asyncio
    async def test_refreshes_on_api_token_expired_401(self, tmp_path):
        """Should refresh when 401 indicates API token is expired."""
        token_file = tmp_path / "token"
        token_data = {
            "access_token": "old",
            "refresh_token": "valid-refresh",
        }
        token_file.write_text(json.dumps(token_data))

        call_count = 0

        class MockTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # Return 401 with API token expiration error
                    return httpx.Response(
                        401,
                        json={
                            "detail": "Invalid API token",
                            "errors": [{"message": "api_token: unauthorized to expired"}],
                        },
                    )
                return httpx.Response(200, json={"success": True})

        new_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        api_response = {
            "token": {
                "access_token": "new-token",
                "refresh_token": "new-refresh",
                "expiry": new_expiry.isoformat(),
            }
        }
        mock_refresh_response = httpx.Response(200, json=api_response)

        transport = TokenRefreshTransport(
            transport=MockTransport(),
            get_org_id=lambda token: None,
        )

        request = httpx.Request(
            "GET",
            "https://api.example.com/test",
            headers={"Authorization": "Bearer old"},
        )

        with patch("openfilter_mcp.auth.get_psctl_token_path", return_value=token_file):
            with patch("openfilter_mcp.auth.httpx.AsyncClient") as mock_client:
                async def async_post(*args, **kwargs):
                    return mock_refresh_response
                mock_client.return_value.__aenter__.return_value.post = async_post

                response = await transport.handle_async_request(request)

        assert response.status_code == 200
        assert call_count == 2  # Original + retry


class TestGetAsyncApiClientWithRetry:
    """Tests for get_async_api_client_with_retry function."""

    def test_raises_authentication_error_without_token(self):
        """Should raise AuthenticationError when no token is available."""
        # Re-import the function to handle module reload from previous tests
        import openfilter_mcp.auth as auth_module
        # Reset the cache to ensure no stale token is returned
        auth_module._reset_token_cache()
        # Patch read_psctl_token to return None to simulate no token
        with patch.object(auth_module, "read_psctl_token", return_value=None):
            with pytest.raises(auth_module.AuthenticationError) as exc_info:
                auth_module.get_async_api_client_with_retry()
            assert "No authentication token available" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_creates_client_with_authorization_header(self):
        """Should create client with Authorization header."""
        with patch("openfilter_mcp.auth.read_psctl_token", return_value="test-token"):
            client = get_async_api_client_with_retry()
            try:
                assert isinstance(client, httpx.AsyncClient)
                assert client.headers["Authorization"] == "Bearer test-token"
                # Should have the base URL set
                assert str(client.base_url).rstrip("/") == get_api_url()
            finally:
                await client.aclose()

    @pytest.mark.asyncio
    async def test_includes_org_header_when_available(self):
        """Should include X-Scope-OrgID header when token has org_id."""
        with patch("openfilter_mcp.auth.read_psctl_token", return_value=SAMPLE_JWT_WITH_ORG):
            client = get_async_api_client_with_retry()
            try:
                assert client.headers["X-Scope-OrgID"] == "48eec17d-3089-4d13-a107-24f5f4cf84c7"
            finally:
                await client.aclose()


class TestIsPlainsightEmployee:
    """Tests for is_plainsight_employee function."""

    def test_returns_true_for_plainsight_email(self):
        """Should return True for users with @plainsight.ai email."""
        assert is_plainsight_employee(PLAINSIGHT_EMPLOYEE_JWT) is True

    def test_returns_false_for_non_plainsight_email(self):
        """Should return False for users without @plainsight.ai email."""
        assert is_plainsight_employee(NON_EMPLOYEE_JWT) is False

    def test_returns_false_for_token_without_email(self):
        """Should return False when token has no email field."""
        assert is_plainsight_employee(SAMPLE_JWT_WITH_ORG) is False

    def test_returns_false_for_invalid_token(self):
        """Should return False for invalid token."""
        assert is_plainsight_employee("invalid-token") is False
        assert is_plainsight_employee("") is False

    def test_case_insensitive_email_check(self):
        """Should match email domain case-insensitively."""
        # The email domain check should be case-insensitive
        assert PLAINSIGHT_EMAIL_DOMAIN == "@plainsight.ai"


class TestGetEffectiveOrgId:
    """Tests for get_effective_org_id function (cross-tenant support)."""

    def test_returns_token_org_when_no_target_specified(self):
        """Should return org ID from token when no target_org_id is specified."""
        org_id = get_effective_org_id(PLAINSIGHT_EMPLOYEE_JWT)
        assert org_id == "plainsight-org-id"

    def test_returns_target_org_for_plainsight_employee(self):
        """Should return target org ID when user is a Plainsight employee."""
        org_id = get_effective_org_id(PLAINSIGHT_EMPLOYEE_JWT, target_org_id="mcdonalds-org-id")
        assert org_id == "mcdonalds-org-id"

    def test_ignores_target_org_for_non_employee(self):
        """Should ignore target_org_id for non-Plainsight employees."""
        org_id = get_effective_org_id(NON_EMPLOYEE_JWT, target_org_id="other-org-id")
        # Should return the user's own org, not the target
        assert org_id == "mcdonalds-org-id"

    def test_returns_token_org_when_target_is_none(self):
        """Should return token org ID when target_org_id is None."""
        org_id = get_effective_org_id(PLAINSIGHT_EMPLOYEE_JWT, target_org_id=None)
        assert org_id == "plainsight-org-id"

    def test_returns_none_for_token_without_org(self):
        """Should return None when token has no org and no target set."""
        org_id = get_effective_org_id(SAMPLE_JWT_NO_ORG)
        assert org_id is None

    def test_returns_target_for_token_without_org_if_employee(self):
        """Should return target org for employee even if token has no org."""
        # Create a JWT with plainsight email but no org
        import base64
        import json
        payload = {"email": "test@plainsight.ai"}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        plainsight_no_org_jwt = f"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.{encoded}.signature"

        org_id = get_effective_org_id(plainsight_no_org_jwt, target_org_id="target-org")
        assert org_id == "target-org"
