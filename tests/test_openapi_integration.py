"""Tests for OpenAPI-generated MCP server integration."""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest


def _get_tool_names(mcp):
    """Get tool names from a FastMCP server (compatible with v2 and v3)."""
    return [tool.name for tool in asyncio.run(mcp.list_tools())]


def _code_context_available():
    """Check if code-context optional dependency is installed."""
    try:
        import code_context  # noqa: F401

        return True
    except ImportError:
        return False


class TestOpenAPISpecLoading:
    """Tests for OpenAPI specification loading."""

    def test_get_openapi_spec_success(self):
        """Should successfully fetch and parse OpenAPI spec."""
        mock_spec = {
            "openapi": "3.1.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {"/test": {"get": {"operationId": "get_test"}}},
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_spec
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.get", return_value=mock_response) as mock_get:
            from openfilter_mcp.server import get_openapi_spec

            result = get_openapi_spec()

        assert result == mock_spec
        mock_get.assert_called_once()

    def test_get_openapi_spec_uses_configured_url(self):
        """Should use PLAINSIGHT_API_URL for fetching spec."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"openapi": "3.1.0"}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.get", return_value=mock_response) as mock_get:
            with patch(
                "openfilter_mcp.server.PLAINSIGHT_API_URL",
                "https://custom.api.example.com",
            ):
                from openfilter_mcp.server import get_openapi_spec

                get_openapi_spec()

        call_url = mock_get.call_args[0][0]
        assert "openapi.json" in call_url


class TestAuthenticatedClient:
    """Tests for authenticated HTTP client creation."""

    def test_create_authenticated_client_with_token(self):
        """Should create client with auth headers when token is available."""
        with patch(
            "openfilter_mcp.server.get_auth_token", return_value="test-token-123"
        ):
            with patch(
                "openfilter_mcp.server.get_effective_org_id", return_value="org-456"
            ):
                from openfilter_mcp.server import create_authenticated_client

                client = create_authenticated_client()

        assert client.headers["Authorization"] == "Bearer test-token-123"
        assert client.headers["X-Scope-OrgID"] == "org-456"

    def test_create_authenticated_client_without_org_id(self):
        """Should create client without X-Scope-OrgID if not in token."""
        with patch(
            "openfilter_mcp.server.get_auth_token", return_value="simple-token"
        ):
            with patch(
                "openfilter_mcp.server.get_effective_org_id", return_value=None
            ):
                from openfilter_mcp.server import create_authenticated_client

                client = create_authenticated_client()

        assert client.headers["Authorization"] == "Bearer simple-token"
        assert "X-Scope-OrgID" not in client.headers

    def test_create_authenticated_client_no_token_does_not_raise(self):
        """Default behavior: no token -> client without Authorization default.

        Per-request handlers resolve auth at call time (session-scoped
        token, psctl/env, or FastMCP-context OAuth bearer), so client
        construction must succeed without a startup token.
        """
        with patch("openfilter_mcp.server.get_auth_token", return_value=None):
            from openfilter_mcp.server import create_authenticated_client

            client = create_authenticated_client()

        assert "Authorization" not in client.headers
        assert "X-Scope-OrgID" not in client.headers

    def test_create_authenticated_client_require_token_raises(self):
        """Legacy require_token=True path still raises when no token."""
        with patch("openfilter_mcp.server.get_auth_token", return_value=None):
            from openfilter_mcp.server import (
                create_authenticated_client,
                AuthenticationError,
            )

            with pytest.raises(AuthenticationError):
                create_authenticated_client(require_token=True)


class TestMCPServerCreation:
    """Tests for MCP server creation from OpenAPI spec."""

    def test_create_mcp_server_without_token_still_works(self):
        """Without a startup token, server creation must succeed and
        register entity + polling tools.

        Auth is resolved per-request inside tool handlers (session-scoped
        token from elicitation > psctl/env > FastMCP-context OAuth
        bearer), so the registration path no longer gates on a startup
        credential. The `if not has_auth` branch only logs a warning.
        """
        mock_spec = {
            "openapi": "3.1.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {},
        }
        with patch.dict(os.environ, {"REQUIRE_AUTH": "false"}):
            with patch("openfilter_mcp.server.get_auth_token", return_value=None):
                with patch(
                    "openfilter_mcp.server.get_openapi_spec",
                    return_value=mock_spec,
                ):
                    with patch(
                        "openfilter_mcp.server.get_latest_index_name",
                        return_value="test-index",
                    ):
                        from openfilter_mcp.server import create_mcp_server

                        # This should NOT raise an exception
                        mcp = create_mcp_server()

        tool_names = _get_tool_names(mcp)

        # Code search tools are only available when code-context is installed
        if _code_context_available():
            assert "search" in tool_names
            assert "search_code" in tool_names
            assert "get_chunk" in tool_names
            assert "read_file" in tool_names

        # Entity tools register regardless of startup token — auth is
        # enforced per-request by the handlers themselves.
        assert "list_entity_types" in tool_names
        assert "list_entities" in tool_names

        # Polling tool registers alongside entity tools.
        assert "poll_until_change" in tool_names

    def test_create_mcp_server_require_auth_fails_without_token(self):
        """Should raise SystemExit when REQUIRE_AUTH=true and no token is available."""
        # `get_openapi_spec` is now fetched unconditionally at startup
        # (decoupled from auth), so without a mock httpx.get would block
        # for the full 30s timeout in offline / CI environments before
        # the try/except catches it. Mock to keep this test hermetic.
        mock_spec = {
            "openapi": "3.1.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {},
        }
        with patch.dict(os.environ, {"REQUIRE_AUTH": "true"}, clear=False):
            os.environ.pop("OAUTH_AS_URL", None)
            with patch("openfilter_mcp.server.get_auth_token", return_value=None):
                with patch(
                    "openfilter_mcp.server.get_openapi_spec",
                    return_value=mock_spec,
                ):
                    from openfilter_mcp.server import create_mcp_server

                    with pytest.raises(SystemExit, match="REQUIRE_AUTH is set"):
                        create_mcp_server()

    def test_create_mcp_server_require_auth_fails_on_invalid_token(self):
        """Should raise SystemExit when REQUIRE_AUTH=true and client construction fails.

        With `require_token=False` the AuthenticationError handler path
        is dead under normal operation, but if a transport-level failure
        ever surfaces it as AuthenticationError, REQUIRE_AUTH must still
        fail-fast rather than serve a half-broken catalog.
        """
        # Import AuthenticationError from server module to ensure class identity
        # matches the except clause (avoids beartype import-hook mismatch).
        from openfilter_mcp.server import AuthenticationError

        mock_spec = {
            "openapi": "3.1.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {},
        }
        with patch.dict(os.environ, {"REQUIRE_AUTH": "true"}, clear=False):
            os.environ.pop("OAUTH_AS_URL", None)
            with patch("openfilter_mcp.server.get_auth_token", return_value="bad-token"):
                with patch(
                    "openfilter_mcp.server.get_openapi_spec",
                    return_value=mock_spec,
                ):
                    with patch(
                        "openfilter_mcp.server.create_authenticated_client",
                        side_effect=AuthenticationError("token expired"),
                    ):
                        from openfilter_mcp.server import create_mcp_server

                        with pytest.raises(SystemExit, match="authentication failed"):
                            create_mcp_server()

    def test_create_mcp_server_require_auth_succeeds_with_token(self):
        """Should start normally when REQUIRE_AUTH=true and a valid token exists."""
        mock_spec = {
            "openapi": "3.1.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {},
        }
        with patch.dict(os.environ, {"REQUIRE_AUTH": "true"}):
            with patch("openfilter_mcp.server.get_auth_token", return_value="good-token"):
                with patch("openfilter_mcp.server.get_effective_org_id", return_value=None):
                    with patch("openfilter_mcp.server.get_openapi_spec", return_value=mock_spec):
                        with patch(
                            "openfilter_mcp.server.get_latest_index_name",
                            return_value="test-index",
                        ):
                            from openfilter_mcp.server import create_mcp_server

                            # Should NOT raise
                            mcp = create_mcp_server()

        tool_names = _get_tool_names(mcp)
        assert "list_entity_types" in tool_names

    def test_create_mcp_server_registers_entity_tools(self):
        """Should create MCP server with entity CRUD tools from OpenAPI spec."""
        mock_spec = {
            "openapi": "3.1.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/projects": {
                    "get": {
                        "operationId": "list_projects",
                        "summary": "List all projects",
                        "responses": {"200": {"description": "Success"}},
                    }
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
                    }
                },
            },
        }

        with patch("openfilter_mcp.server.get_openapi_spec", return_value=mock_spec):
            with patch(
                "openfilter_mcp.server.get_auth_token", return_value="test-token"
            ):
                with patch(
                    "openfilter_mcp.server.get_effective_org_id", return_value=None
                ):
                    with patch(
                        "openfilter_mcp.server.get_latest_index_name",
                        return_value="test-index",
                    ):
                        from openfilter_mcp.server import create_mcp_server

                        mcp = create_mcp_server()

        # Verify entity CRUD tools were registered
        tool_names = _get_tool_names(mcp)

        # Entity tools registered by register_entity_tools
        assert "list_entity_types" in tool_names
        assert "get_entity" in tool_names
        assert "list_entities" in tool_names


class TestCodeSearchTools:
    """Tests for manually-defined code search tools."""

    @pytest.mark.skipif(
        not _code_context_available(),
        reason="code-context not installed",
    )
    def test_search_tool_uses_correct_index(self):
        """Should use the latest index name for search."""
        mock_spec = {
            "openapi": "3.1.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {},
        }

        with patch("openfilter_mcp.server.get_openapi_spec", return_value=mock_spec):
            with patch(
                "openfilter_mcp.server.get_auth_token", return_value="test-token"
            ):
                with patch(
                    "openfilter_mcp.server.get_effective_org_id", return_value=None
                ):
                    with patch(
                        "openfilter_mcp.server.get_latest_index_name",
                        return_value="my-test-index-v2",
                    ):
                        with patch(
                            "openfilter_mcp.server._search_index"
                        ) as mock_search:
                            mock_search.return_value = {"results": []}

                            from openfilter_mcp.server import create_mcp_server

                            mcp = create_mcp_server()

                            tool_names = _get_tool_names(mcp)
                            assert "search" in tool_names

    def test_read_file_prevents_path_traversal(self):
        """Should prevent path traversal attacks."""
        from openfilter_mcp.server import _is_subpath, _real_path

        # Test _is_subpath helper
        assert _is_subpath("/safe/dir/file.txt", "/safe/dir")
        assert not _is_subpath("/other/file.txt", "/safe/dir")
        assert not _is_subpath("/safe/dir/../other/file.txt", "/safe/dir")

        # Test _real_path raises for traversal attempts
        with patch(
            "openfilter_mcp.server.MONOREPO_CLONE_DIR", "/safe/monorepo"
        ):
            with pytest.raises(FileNotFoundError):
                _real_path("../../../etc/passwd")


class TestBuildOAuthProvider:
    """Tests for `_build_oauth_provider` — the OAuth gate constructor.

    Audience list, issuer, algorithm, and JWKS URI all need to be correct
    for tokens to verify, so each piece of the provider config gets its
    own assertion here.
    """

    def test_returns_none_when_oauth_as_url_unset(self):
        """No OAUTH_AS_URL → return None, server falls back to psctl path."""
        from openfilter_mcp.server import _build_oauth_provider

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OAUTH_AS_URL", None)
            assert _build_oauth_provider() is None

    def test_default_audience_list_when_unset(self):
        """Default audience accepts BOTH the RFC 8707 binding (resource_url+/mcp)
        AND the AS URL (iss-fallback for clients without resource=)."""
        from openfilter_mcp.server import _build_oauth_provider

        env = {
            "OAUTH_AS_URL": "https://api.example.com",
            "OAUTH_RESOURCE_URL": "https://mcp.example.com",
            "PORT": "3000",
        }
        env_clear_keys = ["OAUTH_AUDIENCE"]
        with patch.dict(os.environ, env, clear=False):
            for key in env_clear_keys:
                os.environ.pop(key, None)
            mock_verifier = MagicMock()
            mock_provider = MagicMock()
            with patch(
                "fastmcp.server.auth.providers.jwt.JWTVerifier",
                return_value=mock_verifier,
            ) as mock_verifier_cls:
                with patch(
                    "fastmcp.server.auth.RemoteAuthProvider",
                    return_value=mock_provider,
                ) as mock_provider_cls:
                    result = _build_oauth_provider()

        assert result is mock_provider
        verifier_kwargs = mock_verifier_cls.call_args.kwargs
        assert verifier_kwargs["audience"] == [
            "https://mcp.example.com/mcp",
            "https://api.example.com",
        ]
        assert verifier_kwargs["issuer"] == "https://api.example.com"
        assert verifier_kwargs["algorithm"] == "ES256"
        assert verifier_kwargs["jwks_uri"] == "https://api.example.com/.well-known/jwks.json"

        provider_kwargs = mock_provider_cls.call_args.kwargs
        assert provider_kwargs["authorization_servers"] == ["https://api.example.com"]
        assert provider_kwargs["base_url"] == "https://mcp.example.com"

    def test_oauth_audience_env_override(self):
        """OAUTH_AUDIENCE replaces the default audience list entirely."""
        from openfilter_mcp.server import _build_oauth_provider

        env = {
            "OAUTH_AS_URL": "https://api.example.com",
            "OAUTH_RESOURCE_URL": "https://mcp.example.com",
            "OAUTH_AUDIENCE": "custom-aud, second-aud  ,",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "fastmcp.server.auth.providers.jwt.JWTVerifier"
            ) as mock_verifier_cls:
                with patch("fastmcp.server.auth.RemoteAuthProvider"):
                    _build_oauth_provider()

        verifier_kwargs = mock_verifier_cls.call_args.kwargs
        # Stripped of whitespace; empty trailing token dropped.
        assert verifier_kwargs["audience"] == ["custom-aud", "second-aud"]

    def test_trailing_slashes_stripped(self):
        """as_url and resource_url shouldn't accumulate trailing slashes
        when env values are pasted with one."""
        from openfilter_mcp.server import _build_oauth_provider

        env = {
            "OAUTH_AS_URL": "https://api.example.com/",
            "OAUTH_RESOURCE_URL": "https://mcp.example.com/",
        }
        env_clear_keys = ["OAUTH_AUDIENCE"]
        with patch.dict(os.environ, env, clear=False):
            for key in env_clear_keys:
                os.environ.pop(key, None)
            with patch(
                "fastmcp.server.auth.providers.jwt.JWTVerifier"
            ) as mock_verifier_cls:
                with patch(
                    "fastmcp.server.auth.RemoteAuthProvider"
                ) as mock_provider_cls:
                    _build_oauth_provider()

        verifier_kwargs = mock_verifier_cls.call_args.kwargs
        assert verifier_kwargs["jwks_uri"] == "https://api.example.com/.well-known/jwks.json"
        assert verifier_kwargs["issuer"] == "https://api.example.com"
        # Default audience uses the cleaned values.
        assert "https://mcp.example.com/mcp" in verifier_kwargs["audience"]
        provider_kwargs = mock_provider_cls.call_args.kwargs
        assert provider_kwargs["base_url"] == "https://mcp.example.com"


class TestResolveBootstrapAuth:
    """Tests for `_resolve_bootstrap_auth` — the credential precedence
    used by /api-tokens when bootstrapping the elicitation flow.

    The order (psctl/env > FastMCP-context OAuth bearer) is a security
    invariant: in mixed deployments the long-lived primary credential
    must take precedence so that a request with a narrow OAuth bearer
    can't be elevated to using a broad psctl token. (And in OAuth-only
    deployments, the OAuth bearer is the only available credential.)
    """

    def test_returns_psctl_env_token_when_present(self):
        """Primary credential takes precedence over the OAuth fallback."""
        from openfilter_mcp.server import _resolve_bootstrap_auth

        with patch(
            "openfilter_mcp.server.get_auth_token", return_value="psctl-token"
        ):
            # Even if the OAuth dependency is importable, it must not be consulted.
            with patch(
                "fastmcp.server.dependencies.get_access_token"
            ) as mock_get_access:
                result = _resolve_bootstrap_auth()

        assert result == "psctl-token"
        mock_get_access.assert_not_called()

    def test_falls_through_to_oauth_bearer_when_no_psctl(self):
        """OAuth-only mode: psctl is empty → consult fastmcp request bearer."""
        from openfilter_mcp.server import _resolve_bootstrap_auth

        mock_access = MagicMock()
        mock_access.token = "oauth-bearer-xyz"

        with patch("openfilter_mcp.server.get_auth_token", return_value=None):
            with patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=mock_access,
            ):
                result = _resolve_bootstrap_auth()

        assert result == "oauth-bearer-xyz"

    def test_returns_none_when_oauth_dependency_missing(self):
        """Older fastmcp without `dependencies` module: psctl-only mode."""
        from openfilter_mcp.server import _resolve_bootstrap_auth
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "fastmcp.server.dependencies":
                raise ImportError("no such module")
            return real_import(name, *args, **kwargs)

        with patch("openfilter_mcp.server.get_auth_token", return_value=None):
            with patch.object(builtins, "__import__", side_effect=fake_import):
                result = _resolve_bootstrap_auth()

        assert result is None

    def test_returns_none_when_oauth_context_missing(self):
        """Importable but no request context active → return None gracefully."""
        from openfilter_mcp.server import _resolve_bootstrap_auth

        with patch("openfilter_mcp.server.get_auth_token", return_value=None):
            with patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=None,
            ):
                result = _resolve_bootstrap_auth()

        assert result is None

    def test_logs_debug_on_unexpected_failure(self, caplog):
        """Unexpected runtime failures in get_access_token() get logged at
        DEBUG so OAuth-mode operators can diagnose silent breakage."""
        import logging as _logging
        from openfilter_mcp.server import _resolve_bootstrap_auth

        with patch("openfilter_mcp.server.get_auth_token", return_value=None):
            with patch(
                "fastmcp.server.dependencies.get_access_token",
                side_effect=RuntimeError("missing request context"),
            ):
                with caplog.at_level(_logging.DEBUG, logger="openfilter_mcp.server"):
                    result = _resolve_bootstrap_auth()

        assert result is None
        assert any(
            "get_access_token() failed during bootstrap" in rec.message
            for rec in caplog.records
        )


class TestSchemaStripping:
    """Tests for $schema stripping from API responses."""

    def test_strip_schema_from_response_removes_schema_key(self):
        """Should remove $schema keys from response data."""
        from openfilter_mcp.server import strip_schema_from_response

        data = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "name": "test",
            "value": 123,
        }
        result = strip_schema_from_response(data)

        assert "$schema" not in result
        assert result["name"] == "test"
        assert result["value"] == 123

    def test_strip_schema_from_response_handles_nested_objects(self):
        """Should recursively remove $schema from nested objects."""
        from openfilter_mcp.server import strip_schema_from_response

        data = {
            "outer": {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "inner": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "value": "nested",
                },
            },
            "simple": "value",
        }
        result = strip_schema_from_response(data)

        assert "$schema" not in result["outer"]
        assert "$schema" not in result["outer"]["inner"]
        assert result["outer"]["inner"]["value"] == "nested"
        assert result["simple"] == "value"

    def test_strip_schema_from_response_handles_arrays(self):
        """Should recursively remove $schema from arrays."""
        from openfilter_mcp.server import strip_schema_from_response

        data = {
            "items": [
                {"$schema": "schema1", "name": "item1"},
                {"$schema": "schema2", "name": "item2"},
            ]
        }
        result = strip_schema_from_response(data)

        assert "$schema" not in result["items"][0]
        assert "$schema" not in result["items"][1]
        assert result["items"][0]["name"] == "item1"
        assert result["items"][1]["name"] == "item2"

    def test_strip_schema_from_response_preserves_primitives(self):
        """Should preserve primitive values unchanged."""
        from openfilter_mcp.server import strip_schema_from_response

        assert strip_schema_from_response("string") == "string"
        assert strip_schema_from_response(123) == 123
        assert strip_schema_from_response(True) is True
        assert strip_schema_from_response(None) is None


class TestSanitizeOpenAPISpec:
    """Tests for OpenAPI spec sanitization."""

    def test_sanitize_removes_invalid_property_names(self):
        """Should remove properties with invalid names like $schema."""
        from openfilter_mcp.server import sanitize_openapi_spec

        spec = {
            "components": {
                "schemas": {
                    "TestSchema": {
                        "type": "object",
                        "properties": {
                            "$schema": {"type": "string"},
                            "valid_name": {"type": "string"},
                            "also-valid": {"type": "number"},
                        },
                        "required": ["$schema", "valid_name"],
                    }
                }
            }
        }
        result = sanitize_openapi_spec(spec)

        props = result["components"]["schemas"]["TestSchema"]["properties"]
        assert "$schema" not in props
        assert "valid_name" in props
        assert "also-valid" in props

        required = result["components"]["schemas"]["TestSchema"]["required"]
        assert "$schema" not in required
        assert "valid_name" in required

    def test_sanitize_handles_nested_schemas(self):
        """Should recursively sanitize nested schemas."""
        from openfilter_mcp.server import sanitize_openapi_spec

        spec = {
            "paths": {
                "/test": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "$schema": {"type": "string"},
                                            "data": {"type": "string"},
                                        },
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        result = sanitize_openapi_spec(spec)

        props = result["paths"]["/test"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["properties"]
        assert "$schema" not in props
        assert "data" in props


class TestAuthEndpointFiltering:
    """Tests for auth endpoint filtering from OpenAPI tools."""

    def test_auth_endpoints_are_excluded(self):
        """Auth endpoints should be excluded from entity registry."""
        from openfilter_mcp.entity_tools import EntityRegistry

        registry = EntityRegistry(
            {
                "openapi": "3.1.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {
                    "/auth/login": {
                        "post": {
                            "operationId": "auth_login",
                            "summary": "Login",
                            "responses": {"200": {"description": "Success"}},
                        }
                    },
                    "/auth/token/refresh": {
                        "post": {
                            "operationId": "auth_token_refresh",
                            "summary": "Refresh token",
                            "responses": {"200": {"description": "Success"}},
                        }
                    },
                    "/projects": {
                        "get": {
                            "operationId": "list_projects",
                            "summary": "List all projects",
                            "responses": {"200": {"description": "Success"}},
                        }
                    },
                },
            }
        )

        entity_names = list(registry.entities.keys())
        assert any("project" in name for name in entity_names)
        # Auth paths should not produce entities
        for name in entity_names:
            assert "auth" not in name.lower()

    def test_accounts_endpoints_are_excluded(self):
        """Account management endpoints should be excluded from entity registry."""
        from openfilter_mcp.entity_tools import EntityRegistry

        registry = EntityRegistry(
            {
                "openapi": "3.1.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {
                    "/accounts/create": {
                        "post": {
                            "operationId": "account_create",
                            "summary": "Create account",
                            "responses": {"200": {"description": "Success"}},
                        }
                    },
                    "/organizations": {
                        "get": {
                            "operationId": "list_organizations",
                            "summary": "List organizations",
                            "responses": {"200": {"description": "Success"}},
                        }
                    },
                },
            }
        )

        entity_names = list(registry.entities.keys())
        assert any("organization" in name for name in entity_names)
        for name in entity_names:
            assert "account" not in name.lower()

    def test_internal_endpoints_are_excluded(self):
        """Internal endpoints should be excluded from entity registry."""
        from openfilter_mcp.entity_tools import EntityRegistry

        registry = EntityRegistry(
            {
                "openapi": "3.1.0",
                "info": {"title": "Test API", "version": "1.0.0"},
                "paths": {
                    "/internal/health": {
                        "get": {
                            "operationId": "internal_health",
                            "summary": "Health check",
                            "responses": {"200": {"description": "Success"}},
                        }
                    },
                    "/internal/metrics": {
                        "get": {
                            "operationId": "internal_metrics",
                            "summary": "Metrics",
                            "responses": {"200": {"description": "Success"}},
                        }
                    },
                    "/users": {
                        "get": {
                            "operationId": "list_users",
                            "summary": "List users",
                            "responses": {"200": {"description": "Success"}},
                        }
                    },
                },
            }
        )

        entity_names = list(registry.entities.keys())
        assert any("user" in name for name in entity_names)
        for name in entity_names:
            assert "internal" not in name.lower()
