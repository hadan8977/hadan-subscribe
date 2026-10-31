"""Single source of truth for the public MCP tool catalog.

The catalog is deliberately data-only.  Authorization is derived from the
explicit ``TOOL_SCOPES`` mapping below, never from prose descriptions, and the
same schemas are used for ``tools/list`` and server-side argument validation.
"""

from __future__ import annotations

import copy

READ = "quant.read"
ACK = "quant.ack"

_TOOL_DEFINITIONS = [
    {
        "name": "get_subscription_status",
        "description": (
            "Read this account's Quant subscription status, delivery flags, live plans, "
            "payment methods and terms. Available before purchase and changes nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "securitySchemes": [{"type": "oauth2", "scopes": [READ]}],
    },
    {
        "name": "get_scan_schedule",
        "description": (
            "Read exact New York scan and check times for scheduled polling. Follow "
            "the returned windows; this tool does not create a schedule."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "securitySchemes": [{"type": "oauth2", "scopes": [READ]}],
    },
    {
        "name": "prepare_checkout",
        "description": (
            "Prepare one QR order only after the owner explicitly confirms the live "
            "plan, price, payment method and terms in chat. Pass plan_id, method, "
            "accept_terms=true, terms_version and the plan's quote_id from "
            "get_subscription_status. The server fixes the amount; no client amount "
            "or user field is accepted. Show the returned PNG inline; this does not pay."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "minLength": 1},
                "method": {"type": "string", "enum": ["wechat", "alipay"]},
                "accept_terms": {"type": "boolean"},
                "terms_version": {"type": "string", "minLength": 1},
                "quote_id": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "securitySchemes": [{"type": "oauth2", "scopes": [READ]}],
    },
    {
        "name": "get_checkout_status",
        "description": (
            "Read the current checkout order. Poll no faster than every 5 seconds; "
            "paid=true is the only confirmation of payment. This tool never charges."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "securitySchemes": [{"type": "oauth2", "scopes": [READ]}],
    },
    {
        "name": "get_checkout_qr",
        "description": (
            "Return the current checkout QR as a PNG image when the initial image was "
            "not displayed. Never send the owner to a cloud browser."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "securitySchemes": [{"type": "oauth2", "scopes": [READ]}],
    },
    {
        "name": "list_events",
        "description": (
            "Read pending authorized events for on-demand or supported scheduled "
            "checks. Webhook-delivered events are normally hidden while delivery is "
            "active; set include_webhook=true only for an explicit recovery read. "
            "Process and acknowledge with the protocol. An empty list needs no notice."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "after": {"type": "integer", "minimum": 0},
                "include_webhook": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "securitySchemes": [{"type": "oauth2", "scopes": [READ]}],
    },
    {
        "name": "read_content",
        "description": (
            "Read an authorized report version and its attachment manifest. Use the "
            "event's version; withdrawals may revoke access."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_id": {"type": "string", "minLength": 1},
                "version": {"type": "integer", "minimum": 1},
            },
            "required": ["content_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "securitySchemes": [{"type": "oauth2", "scopes": [READ]}],
    },
    {
        "name": "read_attachment",
        "description": (
            "Download one authorized attachment as a base64 embedded resource. "
            "Verify decoded byte size and SHA256 against its manifest."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"attachment_id": {"type": "string", "minLength": 1}},
            "required": ["attachment_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "securitySchemes": [{"type": "oauth2", "scopes": [READ]}],
    },
    {
        "name": "ack_event",
        "description": (
            "Mark an event processed only after its report and every attachment were "
            "processed or durably saved; process withdrawals and account notices "
            "without unavailable content. Idempotent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"event_id": {"type": "string", "minLength": 1}},
            "required": ["event_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "securitySchemes": [{"type": "oauth2", "scopes": [ACK]}],
    },
]

TOOLS = copy.deepcopy(_TOOL_DEFINITIONS)
TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}
TOOL_SCOPES = {
    tool["name"]: tool["securitySchemes"][0]["scopes"][0] for tool in TOOLS
}


class ToolArgumentError(ValueError):
    """A client-supplied tools/call argument violated the published schema."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field


def _check_type(value, expected: str, field: str) -> None:
    valid = {
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }.get(expected)
    if valid is None:
        raise ToolArgumentError("unsupported schema type", field)
    if not valid:
        raise ToolArgumentError(f"{field} must be {expected}", field)


def _validate_value(field: str, value, spec: dict) -> None:
    expected = spec.get("type")
    if expected:
        _check_type(value, expected, field)
    if "enum" in spec and value not in spec["enum"]:
        raise ToolArgumentError(f"{field} has an unsupported value", field)
    if expected == "string" and len(value) < spec.get("minLength", 0):
        raise ToolArgumentError(f"{field} is too short", field)
    if expected == "integer" and "minimum" in spec and value < spec["minimum"]:
        raise ToolArgumentError(f"{field} is below the minimum", field)


def validate_arguments(name: str, arguments) -> dict:
    """Validate and return a tool's arguments.

    ``prepare_checkout`` keeps its explicit legacy compatibility path: an empty
    object uses a previously owner-approved permission.  A non-empty object is
    held to the full live catalog contract.
    """
    tool = TOOL_BY_NAME.get(name)
    if tool is None:
        raise KeyError(name)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolArgumentError("arguments must be an object")
    schema = tool["inputSchema"]
    properties = schema.get("properties", {})
    extra = set(arguments) - set(properties)
    if extra:
        raise ToolArgumentError(
            "unsupported argument: " + sorted(extra)[0], sorted(extra)[0]
        )
    if name == "prepare_checkout":
        required = (
            ["plan_id", "method", "accept_terms", "terms_version"]
            if arguments
            else []
        )
    else:
        required = schema.get("required", [])
    for field in required:
        if field not in arguments:
            raise ToolArgumentError(f"{field} is required", field)
    for field, value in arguments.items():
        _validate_value(field, value, properties[field])
    return arguments
