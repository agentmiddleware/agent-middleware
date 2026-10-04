"""
Service Registry
================

Central registry for MCP-enabled services in the B2A marketplace.
Handles service registration, discovery, and MCP manifest generation.

Architecture:
- Services are registered with their Pydantic input/output schemas
- Schemas are auto-converted to MCP JSON Schema format
- Dynamic MCP proxy routes calls through existing billing layer
"""

from copy import deepcopy
from dataclasses import replace
import inspect
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import UnionType
from typing import Any, Callable, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel
from sqlalchemy import select

from ..db.database import get_session_factory
from ..db.models import ServiceRegistryModel
from ..schemas.billing import ServiceCategory

from .action_permits import (
    ActionToolBinding,
    _check_schema,
    upstream_action_binding_hash,
)

logger = logging.getLogger(__name__)


def _annotation_to_json_schema(annotation: Any) -> dict[str, Any] | None:
    """Translate common callable annotations without inventing a string type."""
    if annotation is Any:
        return {}

    if hasattr(annotation, "model_json_schema"):
        return annotation.model_json_schema()
    if hasattr(annotation, "schema"):
        return annotation.schema()

    primitive_types = {
        str: "string",
        bool: "boolean",
        int: "integer",
        float: "number",
        dict: "object",
        list: "array",
    }
    if annotation in primitive_types:
        return {"type": primitive_types[annotation]}

    origin = get_origin(annotation)
    arguments = get_args(annotation)

    if origin is dict:
        schema: dict[str, Any] = {"type": "object"}
        if len(arguments) == 2:
            value_schema = _annotation_to_json_schema(arguments[1])
            if value_schema is not None:
                schema["additionalProperties"] = value_schema
        return schema

    if origin in (list, set, frozenset):
        schema = {"type": "array"}
        if arguments:
            item_schema = _annotation_to_json_schema(arguments[0])
            if item_schema is not None:
                schema["items"] = item_schema
        return schema

    if origin in (Union, UnionType):
        variants: list[dict[str, Any]] = []
        for argument in arguments:
            if argument is type(None):
                variants.append({"type": "null"})
                continue
            variant = _annotation_to_json_schema(argument)
            if variant is not None:
                variants.append(variant)
        if variants:
            return variants[0] if len(variants) == 1 else {"anyOf": variants}

    return None


def pydantic_to_mcp_schema(model: type[BaseModel] | None) -> dict[str, Any] | None:
    """
    Convert a Pydantic model to MCP JSON Schema format.

    MCP requires full JSON Schema 2020-12 objects for inputSchema.
    Pydantic v2's model_json_schema() is 99% compatible.
    """
    if model is None:
        return None

    if hasattr(model, "model_json_schema"):
        schema = model.model_json_schema()
        return {
            "type": "object",
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
            "additionalProperties": False,
            **{
                k: v
                for k, v in schema.items()
                if k
                not in (
                    "properties",
                    "required",
                    "title",
                    "type",
                )
            },
        }

    if hasattr(model, "schema"):
        schema = model.schema()
        return {
            "type": "object",
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
        }

    return None


def extract_schema_from_callable(
    func: Callable,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """
    Extract input and output schemas from a callable's signature.

    Returns:
        Tuple of (input_schema, output_schema)
    """
    try:
        sig = inspect.signature(func)
        type_hints = get_type_hints(func)

        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue

            if param_name in type_hints:
                hint = type_hints[param_name]
                properties[param_name] = _annotation_to_json_schema(hint) or {}

                if param.default is inspect.Parameter.empty:
                    required.append(param_name)

        input_schema = (
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }
            if properties
            else None
        )

        return_type = type_hints.get("return")
        output_schema = None
        if return_type and return_type is not type(None):
            output_schema = _annotation_to_json_schema(return_type)

        return input_schema, output_schema

    except Exception as e:
        logger.warning(f"Failed to extract schema from {func}: {e}")
        return None, None


def _service_to_mcp_tool(service: dict) -> dict:
    """Convert a service record to MCP tool format."""
    tool = {
        "name": service["service_id"],
        "description": service.get("description", ""),
        "inputSchema": service.get("input_schema")
        or {"type": "object", "properties": {}},
    }

    annotations = {
        "creditsPerCall": service.get("credits_per_unit", 1.0),
        "unitName": service.get("unit_name", "call"),
        "category": service.get("category", "custom"),
    }
    if service.get("credits_per_unit_exact") is not None:
        annotations["creditsPerCallExact"] = service["credits_per_unit_exact"]

    if service.get("require_permit"):
        annotations["requirePermit"] = True

    if service.get("owner_wallet_id"):
        annotations["providerWallet"] = service["owner_wallet_id"]

    # Keep output schema availability as project metadata until calls return and
    # enforce MCP structuredContent against the declared object contract.
    if service.get("output_schema"):
        annotations["hasOutputSchema"] = True

    if service.get("external") or not service.get("is_local", True):
        annotations["external"] = True

    tool["annotations"] = annotations

    return tool


class ServiceRegistry:
    """
    Unified registry for MCP-enabled services.

    Services can be registered with:
    - @mcp_tool decorator (local, in-memory)
    - API endpoint /v1/billing/services (persistent, DB)

    The registry provides:
    - Service discovery for MCP manifests
    - Schema extraction and translation
    - Unified query interface for billing engine
    """

    def __init__(self):
        self._action_bindings: dict[
            str, tuple[ActionToolBinding | None, dict[str, Any]]
        ] = {}
        self._session_factory = get_session_factory
        self._local_registry: dict[str, dict] = {}
        self._func_registry: dict[str, Callable] = {}
        self._executor_registry: dict[str, Any] = {}

    def register_local(
        self,
        service_id: str,
        name: str,
        description: str,
        category: ServiceCategory,
        func: Callable,
        input_model: type[BaseModel] | None = None,
        output_model: type[BaseModel] | None = None,
        credits_per_unit: float = 1.0,
        unit_name: str = "call",
        owner_wallet_id: str | None = None,
        require_permit: bool = False,
    ) -> dict:
        """
        Register a service locally (in-memory) for immediate use.

        This is useful for services defined in the same codebase
        that want MCP-enabled tool exposure without DB persistence.

        When ``require_permit`` is True, governed MCP invocation always
        requires a signed permit + idempotency key — even if legacy
        unpermitted MCP is otherwise allowed.
        """
        input_schema = pydantic_to_mcp_schema(input_model)
        output_schema = pydantic_to_mcp_schema(output_model)

        # Each side falls back independently. Requiring *both* to be absent
        # meant that supplying only an output_model -- a handler with plain
        # typed arguments returning a model -- left the input schema empty, so
        # the tool advertised "takes no arguments" and every obedient client
        # call raised TypeError on the missing parameters.
        if input_schema is None or output_schema is None:
            derived_input, derived_output = extract_schema_from_callable(func)
            if input_schema is None:
                input_schema = derived_input
            if output_schema is None:
                output_schema = derived_output

        service_record = {
            "service_id": service_id,
            "name": name,
            "description": description,
            "category": category.value,
            "credits_per_unit": credits_per_unit,
            "unit_name": unit_name,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "owner_wallet_id": owner_wallet_id,
            "require_permit": bool(require_permit),
            "is_active": True,
            "is_local": True,
            "external": False,
            "is_executable": True,
            "execution_backend": "local",
            "func": func,
            "created_at": datetime.now(timezone.utc),
        }

        self._local_registry[service_id] = service_record
        self._func_registry[service_id] = func

        logger.info(f"Registered local service: {service_id} ({name})")
        return service_record

    def register_upstream(
        self,
        *,
        service_id: str,
        name: str,
        description: str,
        category: ServiceCategory,
        executor: Any,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any] | None,
        credits_per_unit: float,
        upstream_tool_name: str,
        upstream_origin: str,
        credits_per_unit_exact: str | None = None,
        action_binding: ActionToolBinding | None = None,
    ) -> dict[str, Any]:
        """Register one runtime-backed remote MCP tool without persisting secrets."""
        existing = self._local_registry.get(service_id)
        if existing and existing.get("execution_backend") != "upstream_mcp":
            raise ValueError(f"Service ID already registered: {service_id}")

        service_record: dict[str, Any] = {
            "service_id": service_id,
            "name": name,
            "description": description,
            "category": category.value,
            "credits_per_unit": credits_per_unit,
            "credits_per_unit_exact": credits_per_unit_exact,
            "unit_name": "call",
            "input_schema": input_schema,
            "output_schema": output_schema,
            "owner_wallet_id": None,
            "require_permit": True,
            "is_active": True,
            "is_local": False,
            "external": True,
            "is_executable": True,
            "execution_backend": "upstream_mcp",
            "upstream_tool_name": upstream_tool_name,
            "upstream_origin": upstream_origin,
            "created_at": datetime.now(timezone.utc),
        }
        if action_binding is not None:
            if (
                action_binding.public_tool_id != service_id
                or action_binding.input_schema != input_schema
                or not action_binding.deployment_authority
            ):
                raise ValueError("action_binding_registry_mismatch")
            if (
                not re.fullmatch(r"[0-9a-f]{64}", action_binding.upstream_binding_hash)
                or not action_binding.schema_id
                or not action_binding.schema_version
            ):
                raise ValueError("invalid_action_binding")
            expected_hash = upstream_action_binding_hash(
                deployment_authority=action_binding.deployment_authority,
                public_tool_id=service_id,
                upstream_origin=upstream_origin,
                upstream_tool_name=upstream_tool_name,
                schema_id=action_binding.schema_id,
                schema_version=action_binding.schema_version,
                input_schema=input_schema,
            )
            if action_binding.upstream_binding_hash != expected_hash:
                raise ValueError("action_binding_registry_mismatch")
            _check_schema(action_binding.input_schema)
            action_binding = replace(
                action_binding, input_schema=deepcopy(input_schema)
            )
        service_record["input_schema"] = deepcopy(input_schema)
        self._action_bindings[service_id] = (action_binding, deepcopy(service_record))
        self._local_registry[service_id] = service_record
        self._executor_registry[service_id] = executor
        self._func_registry.pop(service_id, None)
        logger.info(
            "Registered upstream MCP service: %s (%s)",
            service_id,
            name,
        )
        return service_record

    async def register_persistent(
        self,
        name: str,
        description: str,
        category: ServiceCategory,
        credits_per_unit: float,
        owner_wallet_id: str,
        unit_name: str = "call",
        input_schema: dict | None = None,
        output_schema: dict | None = None,
        mcp_metadata: dict | None = None,
    ) -> dict:
        """
        Register a service persistently in the database.

        Used by external developers registering billable services
        through the API.
        """
        service_id = f"svc-{uuid.uuid4().hex[:12]}"

        mcp_manifest = {
            "inputSchema": input_schema,
            "outputSchema": output_schema,
            **(mcp_metadata or {}),
        }

        async with self._session_factory()() as session:
            service = ServiceRegistryModel(
                service_id=service_id,
                name=name,
                description=description,
                owner_wallet_id=owner_wallet_id,
                category=category.value,
                credits_per_unit=Decimal(str(credits_per_unit)),
                unit_name=unit_name,
                mcp_manifest=json.dumps(mcp_manifest),
                is_active=True,
            )
            session.add(service)
            await session.commit()

        logger.info(f"Registered persistent service: {service_id} ({name})")

        return {
            "service_id": service_id,
            "name": name,
            "description": description,
            "category": category.value,
            "credits_per_unit": credits_per_unit,
            "unit_name": unit_name,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "owner_wallet_id": owner_wallet_id,
            "is_active": True,
            "is_local": False,
            "external": True,
            "is_executable": False,
            "execution_backend": "metadata_only",
            "created_at": datetime.now(timezone.utc),
        }

    def get_action_binding(self, record: dict[str, Any]) -> ActionToolBinding | None:
        stored = self._action_bindings.get(record.get("service_id", ""))
        if stored is None or stored[0] is None:
            return None
        binding, snapshot = stored
        if binding is None:
            return None
        for field in (
            "service_id",
            "input_schema",
            "execution_backend",
            "upstream_tool_name",
            "upstream_origin",
            "is_executable",
            "is_active",
        ):
            if record.get(field) != snapshot.get(field):
                raise ValueError("action_binding_registry_mismatch")
        return replace(binding, input_schema=deepcopy(binding.input_schema))

    def get_local(self, service_id: str) -> dict | None:
        """Get a locally registered service."""
        return self._local_registry.get(service_id)

    def get_local_func(self, service_id: str) -> Callable | None:
        """Get the callable for a locally registered service."""
        return self._func_registry.get(service_id)

    def get_executor(self, service_id: str) -> Any | None:
        """Get a non-callable runtime executor such as the upstream MCP adapter."""
        return self._executor_registry.get(service_id)

    async def get(self, service_id: str) -> dict | None:
        """Get a service from local or persistent registry."""
        local = self.get_local(service_id)
        if local:
            return local
        return await self.get_persistent(service_id)

    async def get_persistent(self, service_id: str) -> dict | None:
        """Get a persistently registered service from the database."""
        async with self._session_factory()() as session:
            result = await session.execute(
                select(ServiceRegistryModel).where(
                    # ServiceRegistryModel fields are plain Python types (SQLModel),
                    # not Mapped[...], so mypy sees this as a bool comparison rather
                    # than a SQLAlchemy ColumnElement. Root cause: app/db/models.py.
                    ServiceRegistryModel.service_id == service_id  # type: ignore[arg-type]
                )
            )
            service = result.scalar_one_or_none()
            if not service:
                return None

            manifest = {}
            if service.mcp_manifest:
                try:
                    manifest = json.loads(service.mcp_manifest)
                except json.JSONDecodeError:
                    pass

            return {
                "service_id": service.service_id,
                "name": service.name,
                "description": service.description,
                "category": service.category,
                "credits_per_unit": float(service.credits_per_unit),
                "unit_name": service.unit_name,
                "input_schema": manifest.get("inputSchema"),
                "output_schema": manifest.get("outputSchema"),
                "owner_wallet_id": service.owner_wallet_id,
                "is_active": service.is_active,
                "is_local": False,
                "external": True,
                "is_executable": False,
                "execution_backend": "metadata_only",
                "created_at": service.created_at,
            }

    async def list_local(self) -> list[dict]:
        """List all locally registered services."""
        return [
            {k: v for k, v in svc.items() if k not in ("func",)}
            for svc in self._local_registry.values()
        ]

    async def list_persistent(
        self,
        category: ServiceCategory | None = None,
        active_only: bool = True,
    ) -> list[dict]:
        """List all persistently registered services."""
        async with self._session_factory()() as session:
            query = select(ServiceRegistryModel)
            if category:
                # Same plain-Python-type SQLModel field issue as above.
                query = query.where(ServiceRegistryModel.category == category.value)  # type: ignore[arg-type]
            if active_only:
                query = query.where(ServiceRegistryModel.is_active == True)  # type: ignore[arg-type]  # noqa: E712

            result = await session.execute(query)
            services = result.scalars().all()

            results = []
            for service in services:
                manifest = {}
                if service.mcp_manifest:
                    try:
                        manifest = json.loads(service.mcp_manifest)
                    except json.JSONDecodeError:
                        pass

                results.append(
                    {
                        "service_id": service.service_id,
                        "name": service.name,
                        "description": service.description,
                        "category": service.category,
                        "credits_per_unit": float(service.credits_per_unit),
                        "unit_name": service.unit_name,
                        "input_schema": manifest.get("inputSchema"),
                        "output_schema": manifest.get("outputSchema"),
                        "owner_wallet_id": service.owner_wallet_id,
                        "is_active": service.is_active,
                        "is_local": False,
                        "external": True,
                        "is_executable": False,
                        "execution_backend": "metadata_only",
                        "created_at": service.created_at,
                    }
                )

            return results

    async def list_all(self, category: ServiceCategory | None = None) -> list[dict]:
        """List all services (local + persistent)."""
        local = await self.list_local()
        persistent = await self.list_persistent(category=category)

        all_services = local + persistent
        if category:
            all_services = [
                s for s in all_services if s.get("category") == category.value
            ]

        return [s for s in all_services if s.get("is_active", True)]

    async def list_executable(
        self,
        category: ServiceCategory | None = None,
    ) -> list[dict]:
        """List only runtime-executable tools; metadata-only DB rows are omitted."""
        services = await self.list_local()
        if category:
            services = [
                service
                for service in services
                if service.get("category") == category.value
            ]
        return [
            service
            for service in services
            if service.get("is_active", True) and service.get("is_executable", False)
        ]

    async def get_pricing(self, service_id: str) -> tuple[float, str] | None:
        """
        Get pricing for a service. Used by billing engine.

        Returns:
            Tuple of (credits_per_unit, unit_name) or None if not found
        """
        service = await self.get(service_id)
        if service:
            return (
                service.get("credits_per_unit", 1.0),
                service.get("unit_name", "call"),
            )
        return None

    def to_mcp_tool(self, service: dict) -> dict:
        """Convert a service record to MCP tool format."""
        return _service_to_mcp_tool(service)

    def unregister_local(self, service_id: str) -> bool:
        """Unregister a local service."""
        if service_id in self._local_registry:
            del self._local_registry[service_id]
            if service_id in self._func_registry:
                del self._func_registry[service_id]
            self._executor_registry.pop(service_id, None)
            return True
        return False

    def unregister_execution_backend(self, execution_backend: str) -> int:
        """Remove runtime registrations for one backend, returning the count."""
        service_ids = [
            service_id
            for service_id, service in self._local_registry.items()
            if service.get("execution_backend") == execution_backend
        ]
        for service_id in service_ids:
            self.unregister_local(service_id)
        return len(service_ids)


_service_registry: ServiceRegistry | None = None


def get_service_registry() -> ServiceRegistry:
    """Get or create the global ServiceRegistry singleton."""
    global _service_registry
    if _service_registry is None:
        _service_registry = ServiceRegistry()
    return _service_registry
