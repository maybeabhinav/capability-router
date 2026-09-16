"""Stable router errors."""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    INVALID_INPUT = "invalid_input"
    EXECUTION_FAILED = "execution_failed"
    VERIFICATION_FAILED = "verification_failed"
    NOT_FOUND = "not_found"
    SHUTDOWN = "shutdown"
    REFRESH_REQUIRED = "refresh_required"
    UNAVAILABLE = "unavailable"
    POLICY_DENIED = "policy_denied"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    DOWNSTREAM_PROTOCOL_ERROR = "downstream_protocol_error"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal_error"


class RouterError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code.value
        self.message = message
        self.details = details or {}

    def document(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class InputError(RouterError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.INVALID_INPUT, message, details)


class ExecutionError(RouterError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.EXECUTION_FAILED, message, details)


class VerificationError(RouterError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.VERIFICATION_FAILED, message, details)
