"""Entity-based API tools for OpenFilter MCP.

This module provides a consolidated set of CRUD tools that work across all API entities,
dramatically reducing the number of MCP tools exposed (from 100+ to ~7).

Instead of individual tools like `project_create`, `project_get`, `organization_list`, etc.,
we expose generic operations: `create_entity`, `get_entity`, `list_entities`, `update_entity`,
`delete_entity`, plus a discovery tool `list_entity_types`.

Schema validation is performed using JSON Schema validation from the OpenAPI spec.

Cross-tenant support: Plainsight employees (@plainsight.ai) can pass an optional `org_id`
parameter to access resources in other organizations.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import jsonschema
from jsonschema import ValidationError as JsonSchemaValidationError

from openfilter_mcp.auth import get_auth_token, is_plainsight_employee

logger = logging.getLogger(__name__)


@dataclass
class EntityOperation:
    """Represents a single API operation for an entity."""

    method: str  # HTTP method: GET, POST, PUT, PATCH, DELETE
    path: str  # URL path template, e.g., "/projects/{id}"
    operation_id: str  # OpenAPI operation ID
    summary: str  # Human-readable summary
    request_schema: dict | None = None  # JSON Schema for request body
    response_schema: dict | None = None  # JSON Schema for response
    path_params: list[str] = field(default_factory=list)  # Path parameter names
    query_params: dict[str, dict] = field(default_factory=dict)  # Query param schemas
    is_multipart: bool = False  # True if this operation uses multipart/form-data
    file_fields: list[str] = field(default_factory=list)  # Field names for file uploads


@dataclass
class Entity:
    """Represents an API entity with its available operations."""

    name: str  # Entity name, e.g., "project", "organization"
    description: str  # Human-readable description
    operations: dict[str, EntityOperation] = field(default_factory=dict)  # op_type -> operation
    # Common schemas for this entity
    create_schema: dict | None = None
    update_schema: dict | None = None
    response_schema: dict | None = None


class EntityRegistry:
    """Registry of all entities and their operations, built from OpenAPI spec."""

    def __init__(self, openapi_spec: dict):
        self.spec = openapi_spec
        self.entities: dict[str, Entity] = {}
        self._component_schemas = openapi_spec.get("components", {}).get("schemas", {})
        self._parse_spec()

    def _resolve_ref(self, schema: dict, seen: set[str] | None = None) -> dict:
        """Resolve $ref references in a schema, handling circular refs.

        Args:
            schema: The schema to resolve
            seen: Set of already-visited $ref paths to detect cycles
        """
        if not isinstance(schema, dict):
            return schema

        if seen is None:
            seen = set()

        if "$ref" in schema:
            ref_path = schema["$ref"]
            if ref_path.startswith("#/components/schemas/"):
                # Detect circular reference
                if ref_path in seen:
                    # Return the $ref as-is for circular refs (jsonschema handles this)
                    return schema

                schema_name = ref_path.split("/")[-1]
                resolved = self._component_schemas.get(schema_name, {})
                # Track this ref as visited before recursing
                new_seen = seen | {ref_path}
                return self._resolve_ref(resolved, new_seen)
            return schema

        # Recursively resolve refs in nested structures
        result = {}
        for key, value in schema.items():
            if isinstance(value, dict):
                result[key] = self._resolve_ref(value, seen)
            elif isinstance(value, list):
                result[key] = [
                    self._resolve_ref(item, seen) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                result[key] = value
        return result

    # Standard CRUD actions - used to parse operation IDs
    CRUD_ACTIONS = {"list", "get", "create", "update", "delete"}
    # Common custom actions found in REST APIs
    # Note: 'run' is excluded because it appears as a noun in entity names
    # (e.g., 'test-run', 'model-training-run') far more often than as a verb.
    CUSTOM_ACTIONS = {
        "start", "stop", "cancel", "download", "upload", "validate",
        "execute", "export", "check", "apply", "sync", "initiate",
        "invite", "ingest", "restore", "generate",
    }
    ALL_ACTIONS = CRUD_ACTIONS | CUSTOM_ACTIONS

    # Words that should never be singularized (end in 's' but are not plural)
    _NO_SINGULARIZE = {"status", "k8s"}

    def _extract_entity_name(self, path: str, operation_id: str) -> str | None:
        """Extract entity name from path or operation_id.

        Two patterns are recognized in operation IDs:

        1. **Action-first**: ``list-projects``, ``create-filter-subscription``
           → first part is an action verb, the rest is the entity.
        2. **Entity-first**: ``project-list``, ``test-run-get``,
           ``pipeline-version-get-by-name``
           → the *last* action verb separates entity (before) from modifiers
           (after).

        Falls back to path-based extraction if operation_id doesn't match.
        """
        if operation_id:
            op_normalized = operation_id.replace("-", "_")
            parts = op_normalized.split("_")

            if parts[0] in self.ALL_ACTIONS:
                # Action-first: e.g., list_filter_subscriptions,
                # create_model_training_run_purchase, get_filter_readme
                entity_parts = parts[1:]
                if entity_parts:
                    return self._singularize("_".join(entity_parts))
            else:
                # Entity-first: find the *last* action verb; everything
                # before it is the entity name.
                action_idx = None
                for i in range(len(parts) - 1, -1, -1):
                    if parts[i] in self.ALL_ACTIONS:
                        action_idx = i
                        break

                if action_idx is not None and action_idx > 0:
                    entity = "_".join(parts[:action_idx])
                    return self._singularize(entity)

        # Fall back to path-based extraction
        clean_path = re.sub(r"\{[^}]+\}", "", path)
        path_parts = [p for p in clean_path.split("/") if p]

        if path_parts:
            entity = path_parts[-1]
            entity = self._singularize(entity)
            return entity.replace("-", "_")

        return None

    @classmethod
    def _singularize(cls, word: str) -> str:
        """Naive singularization of the last component in a compound name.

        For compound names like ``model_training_run_purchases``, only the
        last segment (``purchases``) is singularized.
        """
        # For compound names, only singularize the last segment
        if "_" in word:
            parts = word.split("_")
            parts[-1] = cls._singularize_word(parts[-1])
            return "_".join(parts)
        return cls._singularize_word(word)

    @classmethod
    def _singularize_word(cls, word: str) -> str:
        """Singularize a single word, with exceptions for words ending in 's'
        that are not actually plural."""
        if word.lower() in cls._NO_SINGULARIZE:
            return word
        if word.endswith("ies") and len(word) > 3:
            return word[:-3] + "y"
        if word.endswith("sses"):
            return word[:-2]  # addresses -> addresse? Actually rare in APIs, skip
        if word.endswith("s") and not word.endswith("ss"):
            return word[:-1]
        return word

    def _classify_operation(self, method: str, path: str, operation_id: str) -> str | None:
        """Classify operation type from operation_id or HTTP method.

        Uses the same action-first / entity-first strategy as _extract_entity_name:
        if the first part is an action, use it; otherwise use the last action verb.
        Falls back to HTTP method heuristics.
        """
        method = method.upper()

        # Try to extract action from operation_id
        if operation_id:
            op_normalized = operation_id.lower().replace("-", "_")
            parts = op_normalized.split("_")

            if parts[0] in self.ALL_ACTIONS:
                # Action-first format: list_filters, create_filter_subscription
                return parts[0]

            # Entity-first format: find the last action verb
            for part in reversed(parts):
                if part in self.ALL_ACTIONS:
                    return part

        # Fall back to HTTP method + path heuristics
        has_id_param = bool(re.search(r"\{[^}]+\}$", path))

        if method == "GET":
            return "get" if has_id_param else "list"
        elif method == "POST":
            return "create"
        elif method in ("PUT", "PATCH"):
            return "update"
        elif method == "DELETE":
            return "delete"

        return None

    def _extract_request_schema(self, operation: dict) -> dict | None:
        """Extract request body schema from operation."""
        request_body = operation.get("requestBody", {})
        content = request_body.get("content", {})

        # Try JSON content types first
        for content_type in ["application/json", "application/cloudevents-batch+json"]:
            if content_type in content:
                schema = content[content_type].get("schema", {})
                return self._resolve_ref(schema)

        # Also check multipart/form-data for file uploads
        if "multipart/form-data" in content:
            schema = content["multipart/form-data"].get("schema", {})
            return self._resolve_ref(schema)

        return None

    def _extract_multipart_info(self, operation: dict) -> tuple[bool, list[str]]:
        """Check if operation uses multipart/form-data and extract file field names."""
        request_body = operation.get("requestBody", {})
        content = request_body.get("content", {})

        if "multipart/form-data" not in content:
            return False, []

        schema = content["multipart/form-data"].get("schema", {})
        properties = schema.get("properties", {})

        # Find fields with format: binary (file uploads)
        file_fields = [
            name
            for name, prop in properties.items()
            if prop.get("format") == "binary" or prop.get("contentEncoding") == "binary"
        ]

        return True, file_fields

    def _extract_response_schema(self, operation: dict) -> dict | None:
        """Extract success response schema from operation."""
        responses = operation.get("responses", {})

        # Look for success responses in priority order
        for status in ["200", "201", "202", "204"]:
            if status in responses:
                response = responses[status]
                content = response.get("content", {})
                if "application/json" in content:
                    schema = content["application/json"].get("schema", {})
                    return self._resolve_ref(schema)

        return None

    def _extract_path_params(self, path: str) -> list[str]:
        """Extract path parameter names from path template."""
        return re.findall(r"\{([^}]+)\}", path)

    def _extract_query_params(self, operation: dict) -> dict[str, dict]:
        """Extract query parameters and their schemas."""
        params = {}
        for param in operation.get("parameters", []):
            if param.get("in") == "query":
                name = param.get("name", "")
                params[name] = {
                    "required": param.get("required", False),
                    "schema": self._resolve_ref(param.get("schema", {})),
                    "description": param.get("description", ""),
                }
        return params

    def _parse_spec(self):
        """Parse OpenAPI spec and build entity registry."""
        paths = self.spec.get("paths", {})

        for path, path_item in paths.items():
            # Skip excluded paths
            if any(
                path.startswith(prefix)
                for prefix in ["/internal", "/auth", "/accounts", "/health", "/ready", "/live", "/metrics", "/probe"]
            ):
                continue

            for method in ["get", "post", "put", "patch", "delete"]:
                if method not in path_item:
                    continue

                operation = path_item[method]
                operation_id = operation.get("operationId", "")
                summary = operation.get("summary", operation.get("description", ""))

                # Extract entity name
                entity_name = self._extract_entity_name(path, operation_id)
                if not entity_name:
                    continue

                # Classify operation type
                op_type = self._classify_operation(method, path, operation_id)
                if not op_type:
                    continue

                # Create or get entity
                if entity_name not in self.entities:
                    self.entities[entity_name] = Entity(
                        name=entity_name,
                        description=f"API operations for {entity_name.replace('_', ' ')}",
                    )

                entity = self.entities[entity_name]

                # Check for multipart/file upload
                is_multipart, file_fields = self._extract_multipart_info(operation)

                # Create operation
                op = EntityOperation(
                    method=method.upper(),
                    path=path,
                    operation_id=operation_id,
                    summary=summary,
                    request_schema=self._extract_request_schema(operation),
                    response_schema=self._extract_response_schema(operation),
                    path_params=self._extract_path_params(path),
                    query_params=self._extract_query_params(operation),
                    is_multipart=is_multipart,
                    file_fields=file_fields,
                )

                # Store operation (may overwrite if multiple paths for same op_type)
                entity.operations[op_type] = op

                # Update entity-level schemas
                if op_type == "create" and op.request_schema:
                    entity.create_schema = op.request_schema
                elif op_type == "update" and op.request_schema:
                    entity.update_schema = op.request_schema
                elif op_type == "get" and op.response_schema:
                    entity.response_schema = op.response_schema

    def get_entity(self, name: str) -> Entity | None:
        """Get entity by name."""
        return self.entities.get(name)

    def list_entities(self) -> list[str]:
        """List all entity names."""
        return sorted(self.entities.keys())

    def get_entity_info(self) -> dict[str, Any]:
        """Get detailed info about all entities for discovery.

        Returns operation-level details including path templates and required parameters,
        giving callers the same information they would get from reading the raw OpenAPI spec.
        """
        result = {}
        for name, entity in sorted(self.entities.items()):
            # Build detailed operation info
            operations_detail = {}
            for op_name, op in entity.operations.items():
                op_info: dict[str, Any] = {
                    "method": op.method,
                    "path": op.path,
                    "summary": op.summary,
                }
                # Only include path_params if there are any
                if op.path_params:
                    op_info["path_params"] = op.path_params
                # Only include query_params if there are any
                if op.query_params:
                    op_info["query_params"] = {
                        k: {"required": v["required"], "description": v.get("description", "")}
                        for k, v in op.query_params.items()
                    }
                # Only include request_schema for operations that have request bodies
                if op.request_schema:
                    op_info["request_schema"] = op.request_schema
                operations_detail[op_name] = op_info

            result[name] = {
                "description": entity.description,
                "operations": operations_detail,
            }
        return result


class EntityToolsHandler:
    """Handler for entity-based CRUD operations with validation.

    Supports cross-tenant access for Plainsight employees via optional org_id parameter.
    """

    def __init__(self, client: httpx.AsyncClient, registry: EntityRegistry):
        self.client = client
        self.registry = registry

    def _validate_schema(self, data: dict, schema: dict | None, context: str) -> list[str]:
        """Validate data against JSON schema, returning list of errors."""
        if not schema:
            return []

        try:
            jsonschema.validate(instance=data, schema=schema)
            return []
        except JsonSchemaValidationError as e:
            return [f"{context}: {e.message}"]

    def _build_path(
        self, path_template: str, path_params: dict[str, str]
    ) -> tuple[str, list[str]]:
        """Build URL path from template and parameters.

        Returns:
            A tuple of (path, missing_params) where missing_params is a list
            of parameter names that were not provided.
        """
        # Use format_map with a custom dict that tracks missing keys
        missing = []

        class TrackingDict(dict):
            def __missing__(self, key):
                missing.append(key)
                return f"{{{key}}}"  # Keep placeholder for error message

        path = path_template.format_map(TrackingDict(path_params))
        return path, missing

    def _handle_response(self, response: httpx.Response) -> dict[str, Any]:
        """Handle HTTP response, returning error dict for failures instead of raising."""
        if response.status_code >= 400:
            try:
                error_body = response.json()
            except Exception:
                error_body = response.text
            return {
                "error": f"API error {response.status_code}",
                "status_code": response.status_code,
                "details": error_body,
            }
        return response.json()

    def _get_headers_for_org(self, org_id: str | None) -> dict[str, str] | None:
        """Get request headers for cross-tenant access.

        Args:
            org_id: Target organization ID for cross-tenant access.

        Returns:
            Headers dict with X-Scope-OrgID if cross-tenant access is allowed,
            None otherwise (uses client's default headers).
        """
        if not org_id:
            return None

        # Check if user is a Plainsight employee
        token = get_auth_token()
        if not token:
            logger.warning("No auth token available for cross-tenant check")
            return None

        if is_plainsight_employee(token):
            logger.debug(f"Cross-tenant access: using org_id {org_id}")
            return {"X-Scope-OrgID": org_id}
        else:
            logger.warning(
                f"Cross-tenant access denied: user is not a Plainsight employee. "
                f"Ignoring org_id={org_id}"
            )
            return None

    async def create(
        self,
        entity_type: str,
        data: dict,
        path_params: dict[str, str] | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new entity."""
        entity = self.registry.get_entity(entity_type)
        if not entity:
            return {
                "error": f"Unknown entity type: {entity_type}",
                "available_entities": self.registry.list_entities(),
            }

        op = entity.operations.get("create")
        if not op:
            return {
                "error": f"Entity '{entity_type}' does not support create operation",
                "available_operations": list(entity.operations.keys()),
            }

        # Validate request data
        errors = self._validate_schema(data, op.request_schema, "Request validation")
        if errors:
            return {"error": "Validation failed", "details": errors, "schema": op.request_schema}

        # Build path - auto-extract path params from data if not provided
        path_params = path_params or {}
        for param in op.path_params:
            if param not in path_params and param in data:
                path_params[param] = data[param]
        path, missing = self._build_path(op.path, path_params)

        if missing:
            return {
                "error": f"Missing required path parameters: {missing}",
                "path_template": op.path,
                "provided_params": path_params,
            }

        # Make request with optional cross-tenant headers
        headers = self._get_headers_for_org(org_id)
        response = await self.client.post(path, json=data, headers=headers)
        return self._handle_response(response)

    async def get(
        self,
        entity_type: str,
        id: str,
        path_params: dict[str, str] | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """Get an entity by ID."""
        entity = self.registry.get_entity(entity_type)
        if not entity:
            return {
                "error": f"Unknown entity type: {entity_type}",
                "available_entities": self.registry.list_entities(),
            }

        op = entity.operations.get("get")
        if not op:
            return {
                "error": f"Entity '{entity_type}' does not support get operation",
                "available_operations": list(entity.operations.keys()),
            }

        # Build path - inject 'id' into last path param if not specified
        path_params = path_params or {}
        if op.path_params and op.path_params[-1] not in path_params:
            path_params[op.path_params[-1]] = id

        path, missing = self._build_path(op.path, path_params)

        if missing:
            return {
                "error": f"Missing required path parameters: {missing}",
                "path_template": op.path,
                "provided_params": path_params,
            }

        # Make request with optional cross-tenant headers
        headers = self._get_headers_for_org(org_id)
        response = await self.client.get(path, headers=headers)
        return self._handle_response(response)

    async def list(
        self,
        entity_type: str,
        filters: dict[str, Any] | None = None,
        path_params: dict[str, str] | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """List entities with optional filters."""
        entity = self.registry.get_entity(entity_type)
        if not entity:
            return {
                "error": f"Unknown entity type: {entity_type}",
                "available_entities": self.registry.list_entities(),
            }

        op = entity.operations.get("list")
        if not op:
            return {
                "error": f"Entity '{entity_type}' does not support list operation",
                "available_operations": list(entity.operations.keys()),
            }

        # Build path - auto-extract path params from filters if not provided
        path_params = path_params or {}
        for param in op.path_params:
            if param not in path_params and filters and param in filters:
                path_params[param] = filters.pop(param)
        path, missing = self._build_path(op.path, path_params)

        if missing:
            return {
                "error": f"Missing required path parameters: {missing}",
                "path_template": op.path,
                "provided_params": path_params,
            }

        # Make request with query params and optional cross-tenant headers
        headers = self._get_headers_for_org(org_id)
        response = await self.client.get(path, params=filters or {}, headers=headers)
        if response.status_code >= 400:
            return self._handle_response(response)

        result = response.json()

        # Wrap list responses in a dict for MCP compatibility
        if isinstance(result, list):
            return {"items": result, "count": len(result)}
        return result

    async def update(
        self,
        entity_type: str,
        id: str,
        data: dict,
        path_params: dict[str, str] | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """Update an entity."""
        entity = self.registry.get_entity(entity_type)
        if not entity:
            return {
                "error": f"Unknown entity type: {entity_type}",
                "available_entities": self.registry.list_entities(),
            }

        op = entity.operations.get("update")
        if not op:
            return {
                "error": f"Entity '{entity_type}' does not support update operation",
                "available_operations": list(entity.operations.keys()),
            }

        # Validate request data
        errors = self._validate_schema(data, op.request_schema, "Request validation")
        if errors:
            return {"error": "Validation failed", "details": errors, "schema": op.request_schema}

        # Build path
        path_params = path_params or {}
        if op.path_params and op.path_params[-1] not in path_params:
            path_params[op.path_params[-1]] = id

        path, missing = self._build_path(op.path, path_params)

        if missing:
            return {
                "error": f"Missing required path parameters: {missing}",
                "path_template": op.path,
                "provided_params": path_params,
            }

        # Make request with optional cross-tenant headers
        headers = self._get_headers_for_org(org_id)

        # Use PATCH or PUT based on operation
        if op.method == "PUT":
            response = await self.client.put(path, json=data, headers=headers)
        else:
            response = await self.client.patch(path, json=data, headers=headers)

        return self._handle_response(response)

    async def delete(
        self,
        entity_type: str,
        id: str,
        path_params: dict[str, str] | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete an entity."""
        entity = self.registry.get_entity(entity_type)
        if not entity:
            return {
                "error": f"Unknown entity type: {entity_type}",
                "available_entities": self.registry.list_entities(),
            }

        op = entity.operations.get("delete")
        if not op:
            return {
                "error": f"Entity '{entity_type}' does not support delete operation",
                "available_operations": list(entity.operations.keys()),
            }

        # Build path
        path_params = path_params or {}
        if op.path_params and op.path_params[-1] not in path_params:
            path_params[op.path_params[-1]] = id

        path, missing = self._build_path(op.path, path_params)

        if missing:
            return {
                "error": f"Missing required path parameters: {missing}",
                "path_template": op.path,
                "provided_params": path_params,
            }

        # Make request with optional cross-tenant headers
        headers = self._get_headers_for_org(org_id)
        response = await self.client.delete(path, headers=headers)

        # Handle 204 No Content
        if response.status_code == 204:
            return {"success": True, "message": f"{entity_type} deleted successfully"}

        return self._handle_response(response)

    async def action(
        self,
        entity_type: str,
        action: str,
        id: str | None = None,
        data: dict | None = None,
        path_params: dict[str, str] | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a custom action on an entity (start, stop, cancel, etc.)."""
        entity = self.registry.get_entity(entity_type)
        if not entity:
            return {
                "error": f"Unknown entity type: {entity_type}",
                "available_entities": self.registry.list_entities(),
            }

        op = entity.operations.get(action)
        if not op:
            return {
                "error": f"Entity '{entity_type}' does not support '{action}' action",
                "available_operations": list(entity.operations.keys()),
            }

        # Validate request data if schema exists and data provided
        if data and op.request_schema:
            errors = self._validate_schema(data, op.request_schema, "Request validation")
            if errors:
                return {"error": "Validation failed", "details": errors, "schema": op.request_schema}

        # Build path
        path_params = path_params or {}
        if id and op.path_params and op.path_params[-1] not in path_params:
            path_params[op.path_params[-1]] = id

        path, missing = self._build_path(op.path, path_params)

        if missing:
            return {
                "error": f"Missing required path parameters: {missing}",
                "path_template": op.path,
                "provided_params": path_params,
            }

        # Get optional cross-tenant headers
        headers = self._get_headers_for_org(org_id)

        # Handle multipart/form-data (file uploads)
        if op.is_multipart and data:
            files = {}
            form_data = {}

            for key, value in (data or {}).items():
                if key in op.file_fields:
                    # Value should be a file path - read and upload
                    if isinstance(value, str):
                        try:
                            with open(value, "rb") as f:
                                file_content = f.read()
                            filename = os.path.basename(value)
                            files[key] = (filename, file_content)
                        except FileNotFoundError:
                            return {"error": f"File not found: {value}"}
                        except IOError as e:
                            return {"error": f"Error reading file {value}: {e}"}
                else:
                    form_data[key] = value

            response = await self.client.post(path, files=files, data=form_data, headers=headers)
        # Make request based on method
        elif op.method == "GET":
            response = await self.client.get(path, params=data or {}, headers=headers)
        elif op.method == "POST":
            response = await self.client.post(path, json=data or {}, headers=headers)
        elif op.method == "PUT":
            response = await self.client.put(path, json=data or {}, headers=headers)
        elif op.method == "PATCH":
            response = await self.client.patch(path, json=data or {}, headers=headers)
        elif op.method == "DELETE":
            response = await self.client.delete(path, headers=headers)
        else:
            return {"error": f"Unsupported HTTP method: {op.method}"}

        if response.status_code == 204:
            return {"success": True, "message": f"{action} completed successfully"}

        return self._handle_response(response)

    def get_entity_schemas(self) -> dict[str, Any]:
        """Get all entity schemas for discovery."""
        return self.registry.get_entity_info()


def register_entity_tools(mcp, client: httpx.AsyncClient, openapi_spec: dict):
    """Register entity-based CRUD tools on an MCP server.

    Args:
        mcp: FastMCP server instance
        client: Authenticated httpx.AsyncClient
        openapi_spec: Parsed OpenAPI specification dict
    """
    registry = EntityRegistry(openapi_spec)
    handler = EntityToolsHandler(client, registry)

    @mcp.tool()
    def list_entity_types() -> dict[str, Any]:
        """List all available API entity types and their operations.

        Returns a dictionary of entity names to their available operations and schemas.
        Use this to discover what entities exist and what you can do with them.
        """
        return handler.get_entity_schemas()

    # Cross-tenant org_id docstring fragment for reuse
    _ORG_ID_DOC = """org_id: Optional organization ID for cross-tenant access (Plainsight employees only).
                If provided, the request will be scoped to the specified organization
                instead of the user's default organization."""

    @mcp.tool()
    async def create_entity(
        entity_type: str,
        data: dict,
        path_params: dict | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new entity of the specified type.

        Args:
            entity_type: The type of entity to create (e.g., 'project', 'organization', 'model').
                        Use list_entity_types() to see available types.
            data: The entity data matching the create schema.
            path_params: Optional path parameters for nested resources
                        (e.g., {'project_id': 'abc123'} for project-scoped entities).
            org_id: Optional organization ID for cross-tenant access (Plainsight employees only).

        Returns:
            The created entity data or an error with validation details.
        """
        return await handler.create(entity_type, data, path_params, org_id)

    @mcp.tool()
    async def get_entity(
        entity_type: str,
        id: str,
        path_params: dict | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """Get an entity by its ID.

        Args:
            entity_type: The type of entity to retrieve.
            id: The entity's unique identifier.
            path_params: Optional path parameters for nested resources.
            org_id: Optional organization ID for cross-tenant access (Plainsight employees only).

        Returns:
            The entity data or an error if not found.
        """
        return await handler.get(entity_type, id, path_params, org_id)

    @mcp.tool()
    async def list_entities(
        entity_type: str,
        filters: dict | None = None,
        path_params: dict | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """List entities of the specified type with optional filtering.

        Args:
            entity_type: The type of entities to list.
            filters: Optional query parameters for filtering/pagination
                    (e.g., {'limit': 10, 'status': 'active'}).
            path_params: Optional path parameters for nested resources.
            org_id: Optional organization ID for cross-tenant access (Plainsight employees only).

        Returns:
            A list of entities or paginated response.
        """
        return await handler.list(entity_type, filters, path_params, org_id)

    @mcp.tool()
    async def update_entity(
        entity_type: str,
        id: str,
        data: dict,
        path_params: dict | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing entity.

        Args:
            entity_type: The type of entity to update.
            id: The entity's unique identifier.
            data: The fields to update (partial update supported).
            path_params: Optional path parameters for nested resources.
            org_id: Optional organization ID for cross-tenant access (Plainsight employees only).

        Returns:
            The updated entity data or an error with validation details.
        """
        return await handler.update(entity_type, id, data, path_params, org_id)

    @mcp.tool()
    async def delete_entity(
        entity_type: str,
        id: str,
        path_params: dict | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete an entity by its ID.

        Args:
            entity_type: The type of entity to delete.
            id: The entity's unique identifier.
            path_params: Optional path parameters for nested resources.
            org_id: Optional organization ID for cross-tenant access (Plainsight employees only).

        Returns:
            Success confirmation or an error.
        """
        return await handler.delete(entity_type, id, path_params, org_id)

    @mcp.tool()
    async def entity_action(
        entity_type: str,
        action: str,
        id: str | None = None,
        data: dict | None = None,
        path_params: dict | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a custom action on an entity (start, stop, cancel, etc.).

        Args:
            entity_type: The type of entity.
            action: The action to perform (e.g., 'start', 'stop', 'cancel').
            id: The entity's unique identifier (if action targets specific entity).
            data: Optional data for the action.
            path_params: Optional path parameters for nested resources.
            org_id: Optional organization ID for cross-tenant access (Plainsight employees only).

        Returns:
            Action result or an error.
        """
        return await handler.action(entity_type, action, id, data, path_params, org_id)
