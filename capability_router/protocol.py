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
    CONTEXT_LIST = "context_list"
    CONTEXT_CURRENT = "context_current"
    CONTEXT_USE = "context_use"
    CONTEXT_EXPLAIN = "context_explain"
    CONTEXT_MOVE = "context_move"
    CONTEXT_UNDO = "context_undo"
    CONTEXT_SHARE = "context_share"
    CONTEXT_UNASSIGN = "context_unassign"


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
    RouterAction.CONTEXT_LIST: (frozenset({"action"}), frozenset()),
    RouterAction.CONTEXT_CURRENT: (frozenset({"action"}), frozenset()),
    RouterAction.CONTEXT_USE: (
        frozenset({"action", "context"}),
        frozenset(),
    ),
    RouterAction.CONTEXT_EXPLAIN: (
        frozenset({"action", "capability_id", "context"}),
        frozenset(),
    ),
    RouterAction.CONTEXT_MOVE: (
        frozenset(
            {
                "action",
                "capability_id",
                "source_context",
                "target_context",
            }
        ),
        frozenset({"expected_revision"}),
    ),
    RouterAction.CONTEXT_UNDO: (
        frozenset({"action", "change_id"}),
        frozenset(),
    ),
    RouterAction.CONTEXT_SHARE: (
        frozenset({"action", "capability_id", "target_context"}),
        frozenset({"expected_revision"}),
    ),
    RouterAction.CONTEXT_UNASSIGN: (
        frozenset({"action", "capability_id", "context"}),
        frozenset({"expected_revision"}),
    ),
}
TOOL_ARGUMENT_FIELDS = frozenset().union(
    *(required | optional for required, optional in ACTION_FIELDS.values())
)
PREFERRED_PROTOCOL_VERSION = ProtocolVersion.V2025_11_25.value
SUPPORTED_PROTOCOL_VERSIONS = frozenset(ProtocolVersion.values())
