"""Check local tool arguments against the advertised ``inputSchema``.

Runs before any permit is minted or reserved, any idempotency record is begun,
and any wallet is charged. Without it a wrong type or an unknown property
reached the tool, the tool raised, and the caller's client error became a
debit, a refund and a ``failed_refunded`` receipt (issue #348).

Only local tools are checked. Their schema is derived from code the gateway
ships (a pydantic input model or the handler's typed signature), so it is what
the handler will actually accept. Upstream and persistent tools carry schemas
supplied by someone else, and the upstream server remains the authority for
those.
"""

from __future__ import annotations

import logging
from typing import Any

from jsonschema import exceptions as jsonschema_exceptions
from jsonschema import validators as jsonschema_validators

from app.trust.adapters import GovernedRequestInvalid

logger = logging.getLogger(__name__)

INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"

# The argument name is client input when it is an unknown property, so it is
# capped before it is echoed back.
_MAX_ARGUMENT_NAME_CHARS = 64


class ToolArgumentsInvalid(GovernedRequestInvalid):
    """The tool arguments do not satisfy the tool's ``inputSchema``.

    The message is fixed. ``argument`` and ``violation`` say which argument
    failed and which schema keyword it failed; the offending value itself is
    never echoed, so a 900,000 character string does not come back in the
    error or land in the log.
    """

    def __init__(self, *, argument: str | None, violation: str) -> None:
        super().__init__("Invalid params: arguments do not match the tool inputSchema")
        self.argument = argument
        self.violation = violation

    def as_error_data(self) -> dict[str, Any]:
        return {
            "reason_code": INVALID_TOOL_ARGUMENTS,
            "argument": self.argument,
            "violation": self.violation,
        }


def _cap(name: str) -> str:
    return name[:_MAX_ARGUMENT_NAME_CHARS]


def _argument_name(error: jsonschema_exceptions.ValidationError) -> str | None:
    path = [str(part) for part in error.absolute_path]
    instance = error.instance
    if error.validator == "additionalProperties" and isinstance(instance, dict):
        properties = error.schema.get("properties", {})
        extras = sorted(str(key) for key in instance if key not in properties)
        if extras:
            path.append(extras[0])
    elif error.validator == "required" and isinstance(instance, dict):
        required = error.validator_value
        if isinstance(required, list):
            missing = [str(name) for name in required if name not in instance]
            if missing:
                path.append(missing[0])
    if not path:
        return None
    return _cap(".".join(path))


def validate_local_tool_arguments(
    service: dict[str, Any] | None, arguments: dict[str, Any]
) -> None:
    """Raise ``ToolArgumentsInvalid`` when ``arguments`` break the tool schema.

    A tool that advertises no schema accepts anything, as before. A schema
    that is not itself valid JSON Schema, or that cannot be evaluated (an
    unresolvable ``$ref``: jsonschema retrieves no remote references by
    default), is a registration bug, not a client error, so it is logged and
    the call proceeds as it did before this check.
    """
    if not service or service.get("execution_backend") != "local":
        return
    schema = service.get("input_schema")
    if not isinstance(schema, dict):
        return
    validator_cls = jsonschema_validators.validator_for(schema)
    try:
        validator_cls.check_schema(schema)
    except jsonschema_exceptions.SchemaError:
        logger.warning(
            "local_tool_input_schema_invalid tool=%s", service.get("service_id")
        )
        return
    try:
        error = jsonschema_exceptions.best_match(
            validator_cls(schema).iter_errors(arguments)
        )
    except Exception:
        logger.warning(
            "local_tool_input_schema_unevaluable tool=%s",
            service.get("service_id"),
            exc_info=True,
        )
        return
    if error is None:
        return
    raise ToolArgumentsInvalid(
        argument=_argument_name(error), violation=str(error.validator)
    )
