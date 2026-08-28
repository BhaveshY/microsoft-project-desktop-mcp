from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from typing import Any, Protocol

from .compat import StrEnum
from .errors import ErrorCode, MspError
from .models import Atomicity, ProjectRef, ProjectState


class LedgerState(StrEnum):
    PENDING_DISPATCH = "pending_dispatch"
    COMMITTED_RECEIPT = "committed_receipt"
    UNKNOWN_COMMIT_STATE = "unknown_commit_state"
    RECONCILIATION = "reconciliation"


@dataclass(frozen=True)
class LedgerEntry:
    session_id: str
    idempotency_key: str
    request_family: str
    fingerprint: str
    state: LedgerState
    result: Any = None
    reconciliation_note: str | None = None


@dataclass(frozen=True)
class ClaimResult:
    entry: LedgerEntry
    acquired: bool


@dataclass(frozen=True)
class PlanRecord:
    plan_id: str
    token: str
    fingerprint: str
    project: ProjectRef
    state_before: ProjectState
    atomicity: Atomicity
    expires_at: datetime
    consumed: bool = False


class OperationLedger(Protocol):
    def lookup(self, session_id: str, idempotency_key: str) -> LedgerEntry | None: ...

    def begin(self, entry: LedgerEntry) -> LedgerEntry: ...

    def claim_dispatch(self, entry: LedgerEntry) -> ClaimResult: ...

    def release_not_dispatched(self, session_id: str, idempotency_key: str, fingerprint: str) -> bool: ...

    def mark_committed(self, session_id: str, idempotency_key: str, result: Any) -> LedgerEntry: ...

    def mark_unknown(self, session_id: str, idempotency_key: str, note: str) -> LedgerEntry: ...

    def begin_reconciliation(self, session_id: str, idempotency_key: str) -> LedgerEntry: ...

    def complete_reconciliation(
        self,
        session_id: str,
        idempotency_key: str,
        *,
        committed: bool,
        result: Any = None,
        note: str | None = None,
    ) -> LedgerEntry: ...

    def store_plan(self, record: PlanRecord) -> None: ...

    def consume_plan(self, token: str, fingerprint: str, now: datetime) -> PlanRecord: ...

    def close(self) -> None: ...


class InMemoryOperationLedger:
    """Replaceable ledger contract. A SQLite implementation can preserve the same state machine."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], LedgerEntry] = {}
        self._plans: dict[str, PlanRecord] = {}
        self._lock = RLock()

    def lookup(self, session_id: str, idempotency_key: str) -> LedgerEntry | None:
        with self._lock:
            return self._entries.get((session_id, idempotency_key))

    def begin(self, entry: LedgerEntry) -> LedgerEntry:
        return self.claim_dispatch(entry).entry

    def claim_dispatch(self, entry: LedgerEntry) -> ClaimResult:
        key = (entry.session_id, entry.idempotency_key)
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                if existing.fingerprint != entry.fingerprint or existing.request_family != entry.request_family:
                    raise MspError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "Idempotency key was used for another request",
                        details={"existing_family": existing.request_family, "new_family": entry.request_family},
                    )
                return ClaimResult(entry=existing, acquired=False)
            self._entries[key] = entry
            return ClaimResult(entry=entry, acquired=True)

    def release_not_dispatched(self, session_id: str, idempotency_key: str, fingerprint: str) -> bool:
        key = (session_id, idempotency_key)
        with self._lock:
            existing = self._entries.get(key)
            if (
                existing is None
                or existing.fingerprint != fingerprint
                or existing.state != LedgerState.PENDING_DISPATCH
            ):
                return False
            del self._entries[key]
            return True

    def _transition(self, session_id: str, idempotency_key: str, **changes: Any) -> LedgerEntry:
        key = (session_id, idempotency_key)
        with self._lock:
            existing = self._entries.get(key)
            if existing is None:
                raise MspError(ErrorCode.INTERNAL_ERROR, "Ledger entry does not exist")
            updated = replace(existing, **changes)
            self._entries[key] = updated
            return updated

    def mark_committed(self, session_id: str, idempotency_key: str, result: Any) -> LedgerEntry:
        return self._transition(
            session_id,
            idempotency_key,
            state=LedgerState.COMMITTED_RECEIPT,
            result=result,
            reconciliation_note=None,
        )

    def mark_unknown(self, session_id: str, idempotency_key: str, note: str) -> LedgerEntry:
        return self._transition(
            session_id,
            idempotency_key,
            state=LedgerState.UNKNOWN_COMMIT_STATE,
            reconciliation_note=note,
        )

    def begin_reconciliation(self, session_id: str, idempotency_key: str) -> LedgerEntry:
        return self._transition(session_id, idempotency_key, state=LedgerState.RECONCILIATION)

    def complete_reconciliation(
        self,
        session_id: str,
        idempotency_key: str,
        *,
        committed: bool,
        result: Any = None,
        note: str | None = None,
    ) -> LedgerEntry:
        return self._transition(
            session_id,
            idempotency_key,
            state=LedgerState.COMMITTED_RECEIPT if committed else LedgerState.UNKNOWN_COMMIT_STATE,
            result=result,
            reconciliation_note=note,
        )

    def store_plan(self, record: PlanRecord) -> None:
        with self._lock:
            self._plans[record.token] = record

    def consume_plan(self, token: str, fingerprint: str, now: datetime) -> PlanRecord:
        with self._lock:
            record = self._plans.get(token)
            if record is None or record.fingerprint != fingerprint or record.consumed:
                raise MspError(ErrorCode.CONFIRMATION_MISMATCH, "Confirmation token does not match an active plan")
            if now >= record.expires_at:
                raise MspError(ErrorCode.CONFIRMATION_EXPIRED, "Confirmation plan has expired")
            consumed = replace(record, consumed=True)
            self._plans[token] = consumed
            return consumed

    def close(self) -> None:
        return None
