"""Authentication module for OpenFilter MCP.

This module provides Bearer token authentication and passthrough to plainsight-api.
Supports both Supabase JWT tokens and API tokens (ps_ prefix).

When no Bearer token is provided in the request, falls back to reading the token
from the psctl CLI configuration at the platform-appropriate location:
  - Linux: ~/.config/plainsight/token
  - macOS: ~/Library/Application Support/plainsight/token
  - Windows: C:\\Users\\<user>\\AppData\\Local\\plainsight\\token
"""

import base64
import json
import logging
import os
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Generator, Optional

import httpx
import platformdirs
from fastmcp.server.auth.providers.debug import DebugTokenVerifier

from openfilter_mcp.redact import register_sensitive

logger = logging.getLogger(__name__)


# Configuration
# Default API URL for the Plainsight API
DEFAULT_API_URL = "https://api.prod.plainsight.tech"


def get_api_url() -> str:
    """Get the API URL from environment variables.

    Checks for environment variables in the following order (psctl-compliant):
    1. PS_API_URL - Primary env var used by psctl CLI
    2. PSCTL_API_URL - Alternative psctl-style naming
    3. PLAINSIGHT_API_URL - Legacy/fallback env var for backwards compatibility

    Returns:
        The API URL to use for plainsight-api requests.
    """
    return (
        os.getenv("PS_API_URL")
        or os.getenv("PSCTL_API_URL")
        or os.getenv("PLAINSIGHT_API_URL")
        or DEFAULT_API_URL
    )


# For backwards compatibility, expose as module-level constant
# Note: This is evaluated at import time. Use get_api_url() for dynamic lookup.
PLAINSIGHT_API_URL = get_api_url()

# psctl token file location (uses platformdirs for cross-platform compatibility)
PSCTL_APP_NAME = "plainsight"
PSCTL_TOKEN_FILENAME = "token"

# Cached token to avoid file I/O races in concurrent async calls
_cached_token: Optional[str] = None
_cached_token_expiry: Optional[datetime] = None
_cached_token_mtime: Optional[float] = None


def _reset_token_cache() -> None:
    """Reset the token cache. Used for testing."""
    global _cached_token, _cached_token_expiry, _cached_token_mtime
    _cached_token = None
    _cached_token_expiry = None
    _cached_token_mtime = None


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


# Plainsight organization identifier (email domain for employees)
PLAINSIGHT_EMAIL_DOMAIN = "@plainsight.ai"


def is_plainsight_employee(token: str) -> bool:
    """Check if the token belongs to a Plainsight employee.

    Plainsight employees are identified by having an email address
    ending with @plainsight.ai.

    Args:
        token: The JWT token string.

    Returns:
        True if the user is a Plainsight employee, False otherwise.
    """
    payload = decode_jwt_payload(token)
    if not payload:
        return False

    # Check email in the token payload
    email = payload.get("email", "")
    if isinstance(email, str) and email.lower().endswith(PLAINSIGHT_EMAIL_DOMAIN):
        return True

    return False


def get_effective_org_id(token: str, target_org_id: Optional[str] = None) -> Optional[str]:
    """Get the effective organization ID to use for API requests.

    For Plainsight employees, allows specifying a target org ID to enable
    cross-tenant operations. Non-employees can only access their own org.

    Args:
        token: The JWT token string.
        target_org_id: Optional target organization ID for cross-tenant access.
                       Only works for Plainsight employees (@plainsight.ai).

    Returns:
        The organization ID to use in X-Scope-OrgID header, or None.
    """
    if target_org_id:
        # Allow cross-tenant operations for Plainsight employees
        if is_plainsight_employee(token):
            logger.debug(
                f"Cross-tenant access enabled: using target org {target_org_id}"
            )
            return target_org_id
        else:
            logger.warning(
                "Target org ID specified but user is not a Plainsight employee. "
                "Cross-tenant access denied."
            )
            # Fall through to use the token's org ID

    return get_org_id_from_token(token)


def get_psctl_token_path() -> Path:
    """Get the path to the psctl token file using platformdirs.

    Uses platformdirs.user_config_dir() for cross-platform compatibility:
      - Linux: ~/.config/plainsight/token
      - macOS: ~/Library/Application Support/plainsight/token
      - Windows: C:\\Users\\<user>\\AppData\\Local\\plainsight\\token

    Returns:
        Path to the token file in the platform-appropriate config directory.
    """
    config_dir = platformdirs.user_config_dir(PSCTL_APP_NAME)
    return Path(config_dir) / PSCTL_TOKEN_FILENAME


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
        with httpx.Client(base_url=get_api_url(), timeout=30.0) as client:
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


async def _async_refresh_token(refresh_token: str) -> Optional[Dict[str, Any]]:
    """Asynchronously refresh an expired access token using the refresh token.

    Args:
        refresh_token: The refresh token string.

    Returns:
        New token data dict (containing access_token, expiry, refresh_token, etc.)
        if successful, None otherwise.
    """
    try:
        async with httpx.AsyncClient(base_url=get_api_url(), timeout=30.0) as client:
            response = await client.post(
                "/auth/token/refresh",
                headers={"Authorization": f"Bearer {refresh_token}"},
            )
            if response.status_code == 200:
                data = response.json()
                # API returns {"token": {...}} wrapper, extract the inner token
                if "token" in data and isinstance(data["token"], dict):
                    return data["token"]
                return data
    except Exception:
        pass
    return None


def _get_refresh_token_from_file() -> Optional[str]:
    """Read the refresh token from the psctl token file.

    Returns:
        The refresh token string if available, None otherwise.
    """
    token_path = get_psctl_token_path()
    if not token_path.exists():
        return None

    try:
        with open(token_path, "r") as f:
            token_data = json.load(f)

        # Handle nested token structure
        if "token" in token_data and isinstance(token_data["token"], dict):
            token_data = token_data["token"]

        return token_data.get("refresh_token")
    except (json.JSONDecodeError, IOError, OSError):
        return None


async def refresh_and_get_new_token() -> Optional[str]:
    """Attempt to refresh the token and return the new access token.

    This is used for transparent token refresh on 401 errors.
    Invalidates the token cache and attempts refresh using the stored refresh token.

    Returns:
        The new access token string if refresh was successful, None otherwise.
    """
    global _cached_token, _cached_token_expiry, _cached_token_mtime

    # Clear the cache to force re-read
    _cached_token = None
    _cached_token_expiry = None
    _cached_token_mtime = None

    # Get refresh token from file
    refresh_token = _get_refresh_token_from_file()
    if not refresh_token:
        logger.debug("No refresh token available for token refresh")
        return None

    # Attempt async refresh
    new_token_data = await _async_refresh_token(refresh_token)
    if not new_token_data:
        logger.warning("Token refresh failed")
        return None

    # Save the new token data
    if _save_token_data(new_token_data):
        new_access_token = new_token_data.get("access_token")
        if new_access_token:
            register_sensitive(new_access_token, label="refreshed-token")
            # Update cache with new token
            _cached_token = new_access_token
            expiry_str = new_token_data.get("expiry")
            if expiry_str:
                try:
                    _cached_token_expiry = datetime.fromisoformat(
                        expiry_str.replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    pass
            logger.debug("Token refreshed successfully")
            return new_access_token

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
    global _cached_token, _cached_token_expiry, _cached_token_mtime

    token_path = get_psctl_token_path()

    # Check file mtime to detect external writes (e.g. `psctl login`)
    try:
        current_mtime = token_path.stat().st_mtime
    except OSError:
        current_mtime = None

    # Return cached token if file hasn't changed and token isn't expiring soon
    if _cached_token and _cached_token_expiry:
        if (
            current_mtime == _cached_token_mtime
            and _cached_token_expiry > datetime.now(timezone.utc) + timedelta(minutes=5)
        ):
            return _cached_token

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

        # Cache the token and file mtime
        _cached_token = access_token
        _cached_token_expiry = expiry
        _cached_token_mtime = current_mtime

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
    """Get the bearer token for API authentication.

    Checks for a token in the following order:
    1. OPENFILTER_TOKEN env var - Pre-scoped API token (e.g., from web portal).
       Takes priority because it's an explicit operator choice for a scoped token
       and enables Docker/CI deployments where no psctl token file exists.
    2. psctl CLI config (~/.config/plainsight/token) - Token from `psctl login`.
       This is the user's full-access token with automatic refresh support.

    Returns:
        The raw bearer token string, or None if not authenticated.
    """
    env_token = os.getenv("OPENFILTER_TOKEN")
    if env_token:
        register_sensitive(env_token, label="env-token")
        return env_token
    token = read_psctl_token()
    if token:
        register_sensitive(token, label="psctl-token")
    return token


def get_api_client(timeout: float = 30.0) -> httpx.Client:
    """Create an HTTP client configured for plainsight-api requests.

    The client automatically includes the Authorization header with the
    bearer token from the current request context, and the X-Scope-OrgID
    header based on the effective organization (supports cross-tenant
    operations for Plainsight employees).

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

    # Add organization ID header (supports cross-tenant for Plainsight employees)
    org_id = get_effective_org_id(token)
    if org_id:
        headers["X-Scope-OrgID"] = org_id

    return httpx.Client(
        base_url=get_api_url(),
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
    header based on the effective organization (supports cross-tenant
    operations for Plainsight employees).

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

    # Add organization ID header (supports cross-tenant for Plainsight employees)
    org_id = get_effective_org_id(token)
    if org_id:
        headers["X-Scope-OrgID"] = org_id

    return httpx.AsyncClient(
        base_url=get_api_url(),
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
        Configured httpx.AsyncClient instance with automatic 401 retry.

    Raises:
        AuthenticationError: If no valid token is available.
    """
    client = get_async_api_client_with_retry(timeout)
    try:
        yield client
    finally:
        await client.aclose()


class TokenRefreshTransport(httpx.AsyncBaseTransport):
    """Custom transport that handles 401 errors by refreshing the token and retrying.

    This transport intercepts 401 Unauthorized responses, attempts to refresh
    the authentication token, and retries the original request with the new token.
    This makes token expiration transparent to the caller.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        get_org_id: callable,
    ):
        """Initialize the token refresh transport.

        Args:
            transport: The underlying transport to use for requests.
            get_org_id: Callable to get org ID from a token.
        """
        self._transport = transport
        self._get_org_id = get_org_id
        self._refresh_lock = None  # Will be initialized lazily

    def _is_token_expired_error(self, response: httpx.Response) -> bool:
        """Check if a 401 response indicates token expiration (vs other auth errors).

        We only want to refresh the token when it's genuinely expired, not when:
        - The user doesn't have permission (should be 403, but check anyway)
        - The token is invalid/malformed
        - The token was revoked

        Token expiration errors from plainsight-api contain specific messages:
        - "token is expired" (from JWT validation)
        - "use expired token" (from auth service)

        Args:
            response: The 401 response to check.

        Returns:
            True if the error indicates token expiration, False otherwise.
        """
        try:
            # Read response body to check error message
            body = response.json()

            # Check the errors array for expiration-related messages
            errors = body.get("errors", [])
            if isinstance(errors, list):
                for error in errors:
                    message = error.get("message", "") if isinstance(error, dict) else str(error)
                    if "expired" in message.lower():
                        return True

            # Also check the detail field
            detail = body.get("detail", "")
            if "expired" in detail.lower():
                return True

        except Exception:
            # If we can't parse the response, don't attempt refresh
            # (could be network error, malformed response, etc.)
            pass

        return False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Handle a request, refreshing token on 401 (expiration only) and retrying.

        Args:
            request: The HTTP request to send.

        Returns:
            The HTTP response.
        """
        response = await self._transport.handle_async_request(request)

        # If we get a 401, check if it's due to token expiration
        if response.status_code == 401:
            # Read the response body to check for expiration
            # We need to read it here because response body can only be read once
            await response.aread()

            if not self._is_token_expired_error(response):
                # Not a token expiration error - return as-is
                # This could be invalid token, revoked token, permission issue, etc.
                logger.debug("Received 401 but not due to token expiration, not refreshing")
                return response

            # Initialize lock lazily to avoid issues with event loop
            if self._refresh_lock is None:
                import asyncio
                self._refresh_lock = asyncio.Lock()

            async with self._refresh_lock:
                logger.debug("Received 401 due to token expiration, attempting refresh")
                new_token = await refresh_and_get_new_token()

                if new_token:
                    # Build new headers with refreshed token
                    new_headers = httpx.Headers(request.headers)
                    new_headers["Authorization"] = f"Bearer {new_token}"

                    # Update org ID header if needed
                    org_id = self._get_org_id(new_token)
                    if org_id:
                        new_headers["X-Scope-OrgID"] = org_id

                    # Create a new request with updated headers
                    new_request = httpx.Request(
                        method=request.method,
                        url=request.url,
                        headers=new_headers,
                        content=request.content,
                    )

                    # Retry the request
                    logger.debug("Retrying request with refreshed token")
                    response = await self._transport.handle_async_request(new_request)

        return response


def get_async_api_client_with_retry(timeout: float = 30.0) -> httpx.AsyncClient:
    """Create an async HTTP client with automatic 401 retry via token refresh.

    This client will transparently handle token expiration by:
    1. Detecting 401 Unauthorized responses
    2. Attempting to refresh the token using the stored refresh token
    3. Retrying the original request with the new token

    Supports cross-tenant operations for Plainsight employees via PS_TARGET_ORG_ID.

    Args:
        timeout: Request timeout in seconds.

    Returns:
        Configured httpx.AsyncClient instance with token refresh transport.

    Raises:
        AuthenticationError: If no valid token is available.
    """
    token = get_auth_token()
    if not token:
        raise AuthenticationError("No authentication token available")

    headers = {"Authorization": f"Bearer {token}"}

    # Add organization ID header (supports cross-tenant for Plainsight employees)
    org_id = get_effective_org_id(token)
    if org_id:
        headers["X-Scope-OrgID"] = org_id

    # Create base transport wrapped with token refresh handling
    base_transport = httpx.AsyncHTTPTransport()
    refresh_transport = TokenRefreshTransport(
        transport=base_transport,
        get_org_id=get_effective_org_id,
    )

    return httpx.AsyncClient(
        base_url=get_api_url(),
        headers=headers,
        timeout=timeout,
        transport=refresh_transport,
    )
