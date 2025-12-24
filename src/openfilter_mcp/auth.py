"""Authentication module for OpenFilter MCP.

This module provides Bearer token authentication and passthrough to plainsight-api.
Supports both Supabase JWT tokens and API tokens (ps_ prefix).

When no Bearer token is provided in the request, falls back to reading the token
from the psctl CLI configuration at ~/.config/plainsight/token (or XDG equivalent).
"""

import base64
import json
import os
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Generator, Optional

import httpx
from fastmcp.server.auth.providers.debug import DebugTokenVerifier


# Configuration
PLAINSIGHT_API_URL = os.getenv("PLAINSIGHT_API_URL", "https://api.prod.plainsight.tech")

# psctl token file location (follows XDG spec)
PSCTL_APP_NAME = "plainsight"
PSCTL_TOKEN_FILENAME = "token"

# Cached token to avoid file I/O races in concurrent async calls
_cached_token: Optional[str] = None
_cached_token_expiry: Optional[datetime] = None


def _reset_token_cache() -> None:
    """Reset the token cache. Used for testing."""
    global _cached_token, _cached_token_expiry
    _cached_token = None
    _cached_token_expiry = None


class AuthenticationError(Exception):
    """Raised when authentication fails or token is missing."""

    pass


def decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    """Decode the payload from a JWT token without verification.

    This is used to extract metadata like organization_id from the token.
    Token validation is performed by plainsight-api.

    Args:
        token: The JWT token string.

    Returns:
        The decoded payload dict, or None if decoding fails.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        payload = parts[1]
        # Add padding if needed
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding

        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def get_org_id_from_token(token: str) -> Optional[str]:
    """Extract the organization ID from a JWT token.

    Looks for organization_id in app_metadata or user_metadata.

    Args:
        token: The JWT token string.

    Returns:
        The organization ID string, or None if not found.
    """
    payload = decode_jwt_payload(token)
    if not payload:
        return None

    # Try app_metadata first, then user_metadata
    for key in ("app_metadata", "user_metadata"):
        metadata = payload.get(key, {})
        if isinstance(metadata, dict):
            org_id = metadata.get("organization_id")
            if org_id:
                return str(org_id)

    return None


def get_psctl_token_path() -> Path:
    """Get the path to the psctl token file following XDG spec.

    Returns:
        Path to the token file (~/.config/plainsight/token or XDG equivalent).
    """
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        config_dir = Path(xdg_config_home)
    else:
        config_dir = Path.home() / ".config"

    return config_dir / PSCTL_APP_NAME / PSCTL_TOKEN_FILENAME


def _refresh_token(refresh_token: str) -> Optional[Dict[str, Any]]:
    """Refresh an expired access token using the refresh token.

    Args:
        refresh_token: The refresh token string.

    Returns:
        New token data dict (containing access_token, expiry, refresh_token, etc.)
        if successful, None otherwise. The returned dict matches the format
        expected by psctl (flat token structure, not nested in a "token" wrapper).
    """
    try:
        with httpx.Client(base_url=PLAINSIGHT_API_URL, timeout=30.0) as client:
            response = client.post(
                "/auth/token/refresh",
                headers={"Authorization": f"Bearer {refresh_token}"},
            )
            if response.status_code == 200:
                data = response.json()
                # API returns {"token": {...}} wrapper, extract the inner token
                # to match psctl's token file format
                if "token" in data and isinstance(data["token"], dict):
                    return data["token"]
                return data
    except Exception:
        pass
    return None


def _save_token_data(token_data: Dict[str, Any]) -> bool:
    """Save token data to the psctl token file.

    Saves in the same format as psctl (flat token structure with access_token,
    refresh_token, expiry, etc.) with secure file permissions (0600).

    Args:
        token_data: The token data dict to save. Should be a flat structure
            containing access_token, refresh_token, expiry, etc.

    Returns:
        True if successful, False otherwise.
    """
    try:
        token_path = get_psctl_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        # Write with secure permissions (0600) like psctl does
        with open(token_path, "w") as f:
            json.dump(token_data, f)
        # Set secure file permissions (owner read/write only)
        os.chmod(token_path, 0o600)
        return True
    except (IOError, OSError):
        return False


def read_psctl_token() -> Optional[str]:
    """Read the access token from the psctl CLI configuration.

    Uses a module-level cache to avoid file I/O races when called concurrently
    from async code. The cache is invalidated when the token is close to expiry.

    This allows users who have authenticated via `psctl login` to use the MCP
    server without additional configuration. If the token is expired but a
    refresh token is available, it will attempt to refresh automatically.

    Returns:
        The access token string if available and valid, None otherwise.
    """
    global _cached_token, _cached_token_expiry

    # Return cached token if valid and not expiring soon
    if _cached_token and _cached_token_expiry:
        if _cached_token_expiry > datetime.now(timezone.utc) + timedelta(minutes=5):
            return _cached_token

    token_path = get_psctl_token_path()

    if not token_path.exists():
        return None

    try:
        with open(token_path, "r") as f:
            token_data = json.load(f)

        # Handle nested token structure from psctl ({"token": {"access_token": ...}})
        if "token" in token_data and isinstance(token_data["token"], dict):
            token_data = token_data["token"]

        access_token = token_data.get("access_token")
        if not access_token:
            return None

        # Check expiry if present
        expiry_str = token_data.get("expiry")
        expiry: Optional[datetime] = None
        if expiry_str:
            try:
                # Parse ISO format datetime
                expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                # Refresh if expired or will expire within 5 minutes
                if expiry < datetime.now(timezone.utc) + timedelta(minutes=5):
                    # Try to refresh the token
                    refresh_token = token_data.get("refresh_token")
                    if refresh_token:
                        new_token_data = _refresh_token(refresh_token)
                        if new_token_data:
                            # Save the new token data
                            _save_token_data(new_token_data)
                            access_token = new_token_data.get("access_token")
                            # Update expiry from new token
                            new_expiry_str = new_token_data.get("expiry")
                            if new_expiry_str:
                                expiry = datetime.fromisoformat(
                                    new_expiry_str.replace("Z", "+00:00")
                                )
                        else:
                            # Refresh failed
                            return None
                    else:
                        # No refresh token available
                        return None
            except (ValueError, TypeError):
                # If we can't parse expiry, still try to use the token
                pass

        # Cache the token
        _cached_token = access_token
        _cached_token_expiry = expiry

        return access_token

    except (json.JSONDecodeError, IOError, OSError):
        return None


def create_token_verifier() -> DebugTokenVerifier:
    """Create a token verifier that passes through all bearer tokens.

    We use DebugTokenVerifier with a permissive validator because plainsight-api
    will perform the actual token validation.

    Returns:
        DebugTokenVerifier configured for token passthrough.
    """
    async def validate_token(token: str) -> bool:
        """Accept all tokens - validation happens at plainsight-api.

        We accept any non-empty token. The actual validation (JWT signature,
        expiration, API key validity) is performed by plainsight-api.
        """
        return bool(token and len(token) > 0)

    return DebugTokenVerifier(
        validate=validate_token,
        client_id="mcp-client",
        scopes=["api:access"],
    )


def get_auth_token() -> Optional[str]:
    """Get the bearer token from psctl config.

    Reads token from psctl CLI config (~/.config/plainsight/token).
    This allows users who have authenticated via `psctl login` to use the MCP
    server without additional configuration.

    Returns:
        The raw bearer token string, or None if not authenticated.
    """
    return read_psctl_token()


def get_api_client(timeout: float = 30.0) -> httpx.Client:
    """Create an HTTP client configured for plainsight-api requests.

    The client automatically includes the Authorization header with the
    bearer token from the current request context, and the X-Scope-OrgID
    header if the token contains organization information.

    Args:
        timeout: Request timeout in seconds.

    Returns:
        Configured httpx.Client instance.

    Raises:
        AuthenticationError: If no valid token is available.
    """
    token = get_auth_token()
    if not token:
        raise AuthenticationError("No authentication token available")

    headers = {"Authorization": f"Bearer {token}"}

    # Add organization ID header if available in the token
    org_id = get_org_id_from_token(token)
    if org_id:
        headers["X-Scope-OrgID"] = org_id

    return httpx.Client(
        base_url=PLAINSIGHT_API_URL,
        headers=headers,
        timeout=timeout,
    )


@contextmanager
def api_client(timeout: float = 30.0) -> Generator[httpx.Client, None, None]:
    """Context manager for plainsight-api requests with automatic cleanup.

    Usage:
        with api_client() as client:
            response = client.get("/some/endpoint")

    Args:
        timeout: Request timeout in seconds.

    Yields:
        Configured httpx.Client instance.

    Raises:
        AuthenticationError: If no valid token is available.
    """
    client = get_api_client(timeout)
    try:
        yield client
    finally:
        client.close()


def get_async_api_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """Create an async HTTP client configured for plainsight-api requests.

    The client automatically includes the Authorization header with the
    bearer token from the current request context, and the X-Scope-OrgID
    header if the token contains organization information.

    Args:
        timeout: Request timeout in seconds.

    Returns:
        Configured httpx.AsyncClient instance.

    Raises:
        AuthenticationError: If no valid token is available.
    """
    token = get_auth_token()
    if not token:
        raise AuthenticationError("No authentication token available")

    headers = {"Authorization": f"Bearer {token}"}

    # Add organization ID header if available in the token
    org_id = get_org_id_from_token(token)
    if org_id:
        headers["X-Scope-OrgID"] = org_id

    return httpx.AsyncClient(
        base_url=PLAINSIGHT_API_URL,
        headers=headers,
        timeout=timeout,
    )


@asynccontextmanager
async def async_api_client(
    timeout: float = 30.0,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async context manager for plainsight-api requests with automatic cleanup.

    Usage:
        async with async_api_client() as client:
            response = await client.get("/some/endpoint")

    Args:
        timeout: Request timeout in seconds.

    Yields:
        Configured httpx.AsyncClient instance.

    Raises:
        AuthenticationError: If no valid token is available.
    """
    client = get_async_api_client(timeout)
    try:
        yield client
    finally:
        await client.aclose()
