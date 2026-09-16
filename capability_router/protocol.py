"""Shared MCP protocol types and metadata."""

from enum import Enum


class WireValue(str, Enum):
    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(item.value for item in cls)


class RouterAction(WireValue):
    SEARCH = "search"
    DESCRIBE = "describe"
    LOAD_SKILL = "load_skill"
    CALL = "call"
    STATUS = "status"


class CapabilityKind(WireValue):
    MCP_TOOL = "mcp_tool"
    SKILL = "skill"


class CapabilityAvailability(WireValue):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class AuditAction(WireValue):
    INVALID = "invalid"


class AuditOutcome(WireValue):
    ALLOWED = "allowed"
    DENIED = "denied"
    ERROR = "error"


class McpMethod(WireValue):
    INITIALIZE = "initialize"
    PING = "ping"
    LIST_TOOLS = "tools/list"
    CALL_TOOL = "tools/call"


class McpNotification(WireValue):
    INITIALIZED = "notifications/initialized"


class TaskSupport(WireValue):
    FORBIDDEN = "forbidden"
    OPTIONAL = "optional"
    REQUIRED = "required"


class ProtocolVersion(WireValue):
    V2025_11_25 = "2025-11-25"
    V2025_06_18 = "2025-06-18"
    V2024_11_05 = "2024-11-05"


ROUTER_ACTIONS = RouterAction.values()
CAPABILITY_KINDS = CapabilityKind.values()
MAX_SEARCH_QUERY_LENGTH = 1024
ACTION_FIELDS = {
    RouterAction.SEARCH: (frozenset({"action", "query"}), frozenset({"kinds", "limit"})),
    RouterAction.DESCRIBE: (frozenset({"action", "capability_id"}), frozenset()),
    RouterAction.LOAD_SKILL: (frozenset({"action", "capability_id"}), frozenset()),
    RouterAction.CALL: (
        frozenset({"action", "capability_id", "arguments"}),
        frozenset(),
    ),
    RouterAction.STATUS: (frozenset({"action"}), frozenset()),
}
TOOL_ARGUMENT_FIELDS = frozenset().union(
    *(required | optional for required, optional in ACTION_FIELDS.values())
)
PREFERRED_PROTOCOL_VERSION = ProtocolVersion.V2025_11_25.value
SUPPORTED_PROTOCOL_VERSIONS = frozenset(ProtocolVersion.values())
