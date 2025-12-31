"""Tests for entity-based API tools."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from openfilter_mcp.entity_tools import (
    EntityRegistry,
    EntityToolsHandler,
    Entity,
    EntityOperation,
)


# Sample OpenAPI spec for testing
SAMPLE_OPENAPI_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Test API", "version": "1.0.0"},
    "components": {
        "schemas": {
            "Project": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string", "minLength": 1, "maxLength": 50},
                    "description": {"type": "string"},
                },
                "required": ["name"],
            },
            "Organization": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                },
            },
        }
    },
    "paths": {
        "/projects": {
            "get": {
                "operationId": "project_list",
                "summary": "List all projects",
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/Project"},
                                }
                            }
                        },
                    }
                },
            },
            "post": {
                "operationId": "project_create",
                "summary": "Create a project",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Project"}
                        }
                    }
                },
                "responses": {
                    "201": {
                        "description": "Created",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Project"}
                            }
                        },
                    }
                },
            },
        },
        "/projects/{id}": {
            "get": {
                "operationId": "project_get",
                "summary": "Get a project",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Project"}
                            }
                        },
                    }
                },
            },
            "patch": {
                "operationId": "project_update",
                "summary": "Update a project",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Project"}
                        }
                    }
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Project"}
                            }
                        },
                    }
                },
            },
            "delete": {
                "operationId": "project_delete",
                "summary": "Delete a project",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"204": {"description": "No Content"}},
            },
        },
        "/organizations": {
            "get": {
                "operationId": "organization_list",
                "summary": "List organizations",
                "responses": {"200": {"description": "OK"}},
            },
        },
        "/organizations/{id}": {
            "get": {
                "operationId": "organization_get",
                "summary": "Get organization",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "OK"}},
            },
        },
        # Nested resource
        "/projects/{project_id}/models": {
            "get": {
                "operationId": "model_list",
                "summary": "List models in project",
                "parameters": [
                    {"name": "project_id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "OK"}},
            },
            "post": {
                "operationId": "model_create",
                "summary": "Create model",
                "parameters": [
                    {"name": "project_id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                                "required": ["name"],
                            }
                        }
                    }
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
        # Custom action
        "/trainings/{id}/cancel": {
            "post": {
                "operationId": "training_cancel",
                "summary": "Cancel training",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "OK"}},
            },
        },
        # Excluded paths (should not be parsed)
        "/internal/metrics": {
            "get": {
                "operationId": "internal_metrics",
                "summary": "Internal metrics",
                "responses": {"200": {"description": "OK"}},
            },
        },
        "/auth/login": {
            "post": {
                "operationId": "auth_login",
                "summary": "Login",
                "responses": {"200": {"description": "OK"}},
            },
        },
    },
}


class TestEntityRegistry:
    """Tests for EntityRegistry."""

    def test_parse_entities(self):
        """Test that entities are parsed from OpenAPI spec."""
        registry = EntityRegistry(SAMPLE_OPENAPI_SPEC)

        # Should find project, organization, model, training entities
        entities = registry.list_entities()
        assert "project" in entities
        assert "organization" in entities
        assert "model" in entities
        assert "training" in entities

        # Should not include excluded paths
        assert "internal" not in entities
        assert "auth" not in entities

    def test_project_operations(self):
        """Test that project entity has correct operations."""
        registry = EntityRegistry(SAMPLE_OPENAPI_SPEC)
        project = registry.get_entity("project")

        assert project is not None
        assert "list" in project.operations
        assert "get" in project.operations
        assert "create" in project.operations
        assert "update" in project.operations
        assert "delete" in project.operations

    def test_organization_operations(self):
        """Test that organization entity has correct operations."""
        registry = EntityRegistry(SAMPLE_OPENAPI_SPEC)
        org = registry.get_entity("organization")

        assert org is not None
        assert "list" in org.operations
        assert "get" in org.operations

    def test_custom_action(self):
        """Test that custom actions are detected."""
        registry = EntityRegistry(SAMPLE_OPENAPI_SPEC)
        training = registry.get_entity("training")

        assert training is not None
        assert "cancel" in training.operations

    def test_path_params_extracted(self):
        """Test that path parameters are extracted correctly."""
        registry = EntityRegistry(SAMPLE_OPENAPI_SPEC)
        project = registry.get_entity("project")

        get_op = project.operations["get"]
        assert "id" in get_op.path_params

    def test_request_schema_extracted(self):
        """Test that request schemas are extracted and resolved."""
        registry = EntityRegistry(SAMPLE_OPENAPI_SPEC)
        project = registry.get_entity("project")

        create_op = project.operations["create"]
        assert create_op.request_schema is not None
        assert "properties" in create_op.request_schema
        assert "name" in create_op.request_schema["properties"]

    def test_entity_info_for_discovery(self):
        """Test get_entity_info returns usable discovery data."""
        registry = EntityRegistry(SAMPLE_OPENAPI_SPEC)
        info = registry.get_entity_info()

        assert "project" in info
        assert "operations" in info["project"]
        assert set(info["project"]["operations"]) == {"list", "get", "create", "update", "delete"}


class TestEntityToolsHandler:
    """Tests for EntityToolsHandler."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock httpx.AsyncClient."""
        client = AsyncMock()
        return client

    @pytest.fixture
    def handler(self, mock_client):
        """Create handler with mock client."""
        registry = EntityRegistry(SAMPLE_OPENAPI_SPEC)
        return EntityToolsHandler(mock_client, registry)

    @pytest.mark.asyncio
    async def test_create_entity_success(self, handler, mock_client):
        """Test successful entity creation."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "123", "name": "Test Project"}
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response

        result = await handler.create("project", {"name": "Test Project"})

        assert result == {"id": "123", "name": "Test Project"}
        mock_client.post.assert_called_once_with("/projects", json={"name": "Test Project"})

    @pytest.mark.asyncio
    async def test_create_entity_validation_error(self, handler, mock_client):
        """Test entity creation with invalid data."""
        # Name is required but missing
        result = await handler.create("project", {})

        assert "error" in result
        assert "Validation failed" in result["error"]

    @pytest.mark.asyncio
    async def test_create_unknown_entity(self, handler, mock_client):
        """Test creating an unknown entity type."""
        result = await handler.create("nonexistent", {"name": "test"})

        assert "error" in result
        assert "Unknown entity type" in result["error"]
        assert "available_entities" in result

    @pytest.mark.asyncio
    async def test_get_entity_success(self, handler, mock_client):
        """Test getting an entity by ID."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "123", "name": "Test Project"}
        mock_response.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_response

        result = await handler.get("project", "123")

        assert result == {"id": "123", "name": "Test Project"}
        mock_client.get.assert_called_once_with("/projects/123")

    @pytest.mark.asyncio
    async def test_list_entities_success(self, handler, mock_client):
        """Test listing entities - arrays are wrapped in dict for MCP compatibility."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"id": "1"}, {"id": "2"}]
        mock_response.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_response

        result = await handler.list("project", filters={"limit": 10})

        # List responses are wrapped in a dict with items and count
        assert result == {"items": [{"id": "1"}, {"id": "2"}], "count": 2}
        mock_client.get.assert_called_once_with("/projects", params={"limit": 10})

    @pytest.mark.asyncio
    async def test_update_entity_success(self, handler, mock_client):
        """Test updating an entity."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "123", "name": "Updated"}
        mock_response.raise_for_status = MagicMock()
        mock_client.patch.return_value = mock_response

        result = await handler.update("project", "123", {"name": "Updated"})

        assert result == {"id": "123", "name": "Updated"}
        mock_client.patch.assert_called_once_with("/projects/123", json={"name": "Updated"})

    @pytest.mark.asyncio
    async def test_delete_entity_success(self, handler, mock_client):
        """Test deleting an entity."""
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.raise_for_status = MagicMock()
        mock_client.delete.return_value = mock_response

        result = await handler.delete("project", "123")

        assert result["success"] is True
        mock_client.delete.assert_called_once_with("/projects/123")

    @pytest.mark.asyncio
    async def test_action_success(self, handler, mock_client):
        """Test executing a custom action."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "cancelled"}
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response

        result = await handler.action("training", "cancel", id="123")

        assert result == {"status": "cancelled"}
        mock_client.post.assert_called_once_with("/trainings/123/cancel", json={})

    @pytest.mark.asyncio
    async def test_nested_resource_with_path_params(self, handler, mock_client):
        """Test creating nested resource with path parameters."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "model-1", "name": "My Model"}
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response

        result = await handler.create(
            "model",
            {"name": "My Model"},
            path_params={"project_id": "proj-123"},
        )

        assert result == {"id": "model-1", "name": "My Model"}
        mock_client.post.assert_called_once_with(
            "/projects/proj-123/models", json={"name": "My Model"}
        )


class TestSchemaValidation:
    """Tests for JSON Schema validation."""

    @pytest.fixture
    def handler(self):
        """Create handler with mock client."""
        mock_client = AsyncMock()
        registry = EntityRegistry(SAMPLE_OPENAPI_SPEC)
        return EntityToolsHandler(mock_client, registry)

    def test_validate_valid_data(self, handler):
        """Test validation passes for valid data."""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        errors = handler._validate_schema({"name": "test"}, schema, "Test")
        assert errors == []

    def test_validate_missing_required_field(self, handler):
        """Test validation fails for missing required field."""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        errors = handler._validate_schema({}, schema, "Test")
        assert len(errors) == 1
        assert "name" in errors[0].lower()

    def test_validate_wrong_type(self, handler):
        """Test validation fails for wrong type."""
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
        }
        errors = handler._validate_schema({"count": "not a number"}, schema, "Test")
        assert len(errors) == 1

    def test_validate_no_schema(self, handler):
        """Test validation passes when no schema is provided."""
        errors = handler._validate_schema({"anything": "goes"}, None, "Test")
        assert errors == []


class TestPathBuilding:
    """Tests for URL path building."""

    @pytest.fixture
    def handler(self):
        """Create handler with mock client."""
        mock_client = AsyncMock()
        registry = EntityRegistry(SAMPLE_OPENAPI_SPEC)
        return EntityToolsHandler(mock_client, registry)

    def test_build_simple_path(self, handler):
        """Test building path without parameters."""
        path = handler._build_path("/projects", {})
        assert path == "/projects"

    def test_build_path_with_single_param(self, handler):
        """Test building path with single parameter."""
        path = handler._build_path("/projects/{id}", {"id": "123"})
        assert path == "/projects/123"

    def test_build_path_with_multiple_params(self, handler):
        """Test building path with multiple parameters."""
        path = handler._build_path(
            "/projects/{project_id}/models/{model_id}",
            {"project_id": "proj-1", "model_id": "model-2"},
        )
        assert path == "/projects/proj-1/models/model-2"
