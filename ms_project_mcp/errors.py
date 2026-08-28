from __future__ import annotations

from .compat import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    SESSION_NOT_FOUND = "session_not_found"
    PROJECT_CLOSED = "project_closed"
    PROJECT_IDENTITY_CHANGED = "project_identity_changed"
    STALE_STATE = "stale_state"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_MISMATCH = "confirmation_mismatch"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    DEPENDENCY_CYCLE = "dependency_cycle"
    OWNERSHIP_VIOLATION = "ownership_violation"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BACKEND_EXECUTION_FAILED = "backend_execution_failed"
    WRITE_ROLLED_BACK = "write_rolled_back"
    VERIFICATION_FAILED = "verification_failed"
    REQUEST_IN_PROGRESS = "request_in_progress"
    UNKNOWN_COMMIT_STATE = "unknown_commit_state"
    INTERNAL_ERROR = "internal_error"


class MspError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class DispatchState(StrEnum):
    NOT_DISPATCHED = "not_dispatched"
    MAY_HAVE_DISPATCHED = "may_have_dispatched"


class BackendExecutionError(MspError):
    def __init__(
        self,
        message: str,
        *,
        dispatch_state: DispatchState,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.dispatch_state = dispatch_state
        merged_details = {**(details or {}), "dispatch_state": dispatch_state.value}
        super().__init__(
            ErrorCode.BACKEND_EXECUTION_FAILED,
            message,
            retryable=dispatch_state == DispatchState.NOT_DISPATCHED if retryable is None else retryable,
            details=merged_details,
        )
