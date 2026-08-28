from __future__ import annotations

import base64
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable

from pydantic import BaseModel

from . import models
from .errors import ErrorCode, MspError
from .ledger import ClaimResult, LedgerEntry, LedgerState, OperationLedger, PlanRecord
from .models import Atomicity, ProjectRef, ProjectState
from .persistence import apply_user_only_acl, ensure_state_dir


SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 10_000
ORPHAN_NOTE = "process_terminated_before_dispatch_receipt"


def _model_types() -> dict[str, type[BaseModel]]:
    result: dict[str, type[BaseModel]] = {}
    for value in vars(models).values():
        if isinstance(value, type) and issubclass(value, BaseModel):
            result[value.__name__] = value
    return result


_MODEL_TYPES = _model_types()


def _encode_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"kind": "none"}
    if isinstance(value, bool):
        return {"kind": "bool", "value": value}
    if isinstance(value, int):
        return {"kind": "int", "value": value}
    if isinstance(value, float):
        return {"kind": "float", "value": value}
    if isinstance(value, str):
        return {"kind": "str", "value": value}
    if isinstance(value, bytes):
        return {"kind": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Decimal):
        return {"kind": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"kind": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"kind": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"kind": "time", "value": value.isoformat()}
    if isinstance(value, Enum):
        return {"kind": "enum", "value": value.value}
    if isinstance(value, BaseModel):
        return {
            "kind": "model",
            "type": type(value).__name__,
            "value": value.model_dump(mode="json"),
        }
    if isinstance(value, tuple):
        return {"kind": "tuple", "value": [_encode_value(item) for item in value]}
    if isinstance(value, list):
        return {"kind": "list", "value": [_encode_value(item) for item in value]}
    if isinstance(value, dict):
        return {
            "kind": "dict",
            "value": [[_encode_value(key), _encode_value(item)] for key, item in value.items()],
        }
    raise TypeError(f"Ledger result is not JSON-serializable: {type(value).__name__}")


def _decode_value(envelope: dict[str, Any]) -> Any:
    kind = envelope.get("kind")
    value = envelope.get("value")
    if kind == "none":
        return None
    if kind in {"bool", "int", "float", "str"}:
        return value
    if kind == "bytes":
        return base64.b64decode(value)
    if kind == "decimal":
        return Decimal(value)
    if kind == "datetime":
        return datetime.fromisoformat(value)
    if kind == "date":
        return date.fromisoformat(value)
    if kind == "time":
        return time.fromisoformat(value)
    if kind == "enum":
        return value
    if kind == "model":
        model_type = _MODEL_TYPES.get(envelope.get("type"))
        if model_type is None:
            raise RuntimeError("Ledger contains an unknown typed result")
        return model_type.model_validate(value)
    if kind == "tuple":
        return tuple(_decode_value(item) for item in value)
    if kind == "list":
        return [_decode_value(item) for item in value]
    if kind == "dict":
        return {_decode_value(key): _decode_value(item) for key, item in value}
    raise RuntimeError("Ledger contains an invalid result envelope")


def _serialize_result(value: Any) -> str:
    return json.dumps(_encode_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _deserialize_result(value: str | None) -> Any:
    if value is None:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise RuntimeError("Ledger result envelope must be an object")
    return _decode_value(parsed)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_alive(pid: int) -> bool:
    """Read process state without using Windows ``os.kill`` semantics."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # An invalid PID is definitely gone. Access-denied and unusual failures
        # are treated as alive so startup recovery never guesses that a live
        # writer is dead.
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


class SQLiteOperationLedger(OperationLedger):
    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        owner_pid: int | None = None,
        process_alive: Callable[[int], bool] = _process_alive,
        acl_applier: Callable[[Path], None] = apply_user_only_acl,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path).expanduser().resolve()
        ensure_state_dir(self.path.parent)
        self._busy_timeout_ms = busy_timeout_ms
        self._owner_pid = os.getpid() if owner_pid is None else owner_pid
        self._process_alive = process_alive
        self._acl_applier = acl_applier
        self._initialize()

    def _protect_storage(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            if not candidate.exists():
                continue
            try:
                self._acl_applier(candidate)
            except Exception:
                pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        deadline = monotonic() + (self._busy_timeout_ms / 1000)
        while True:
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or monotonic() >= deadline:
                    connection.close()
                    raise
                sleep(0.01)
        connection.execute("PRAGMA foreign_keys=ON")
        self._protect_storage()
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if version is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(version["value"]) != SCHEMA_VERSION:
                connection.rollback()
                raise RuntimeError(f"Unsupported operation ledger schema version: {version['value']}")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    session_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_family TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    reconciliation_note TEXT,
                    owner_pid INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(session_id, idempotency_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    token TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    state_token TEXT NOT NULL,
                    atomicity TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0, 1)),
                    consumed_at TEXT
                )
                """
            )
            connection.commit()
        self._recover_orphans()

    def _recover_orphans(self) -> None:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT owner_pid FROM operations WHERE state=?",
                (LedgerState.PENDING_DISPATCH.value,),
            ).fetchall()
            orphan_pids = [int(row["owner_pid"]) for row in rows if not self._process_alive(int(row["owner_pid"]))]
            if not orphan_pids:
                return
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in orphan_pids)
            connection.execute(
                f"""
                UPDATE operations
                SET state=?, reconciliation_note=?, updated_at=CURRENT_TIMESTAMP
                WHERE state=? AND owner_pid IN ({placeholders})
                """,
                (
                    LedgerState.UNKNOWN_COMMIT_STATE.value,
                    ORPHAN_NOTE,
                    LedgerState.PENDING_DISPATCH.value,
                    *orphan_pids,
                ),
            )
            connection.commit()

    @staticmethod
    def _entry(row: sqlite3.Row) -> LedgerEntry:
        return LedgerEntry(
            session_id=row["session_id"],
            idempotency_key=row["idempotency_key"],
            request_family=row["request_family"],
            fingerprint=row["fingerprint"],
            state=LedgerState(row["state"]),
            result=_deserialize_result(row["result_json"]),
            reconciliation_note=row["reconciliation_note"],
        )

    def lookup(self, session_id: str, idempotency_key: str) -> LedgerEntry | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE session_id=? AND idempotency_key=?",
                (session_id, idempotency_key),
            ).fetchone()
        return None if row is None else self._entry(row)

    def begin(self, entry: LedgerEntry) -> LedgerEntry:
        return self.claim_dispatch(entry).entry

    def claim_dispatch(self, entry: LedgerEntry) -> ClaimResult:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO operations(
                    session_id, idempotency_key, request_family, fingerprint, state,
                    result_json, reconciliation_note, owner_pid
                ) VALUES(?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    entry.session_id,
                    entry.idempotency_key,
                    entry.request_family,
                    entry.fingerprint,
                    LedgerState.PENDING_DISPATCH.value,
                    self._owner_pid,
                ),
            )
            acquired = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM operations WHERE session_id=? AND idempotency_key=?",
                (entry.session_id, entry.idempotency_key),
            ).fetchone()
            connection.commit()
        assert row is not None
        existing = self._entry(row)
        if existing.fingerprint != entry.fingerprint or existing.request_family != entry.request_family:
            raise MspError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Idempotency key was used for another request",
                details={"existing_family": existing.request_family, "new_family": entry.request_family},
            )
        return ClaimResult(entry=existing, acquired=acquired)

    def release_not_dispatched(self, session_id: str, idempotency_key: str, fingerprint: str) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM operations
                WHERE session_id=? AND idempotency_key=? AND fingerprint=? AND state=?
                """,
                (session_id, idempotency_key, fingerprint, LedgerState.PENDING_DISPATCH.value),
            )
            connection.commit()
            return cursor.rowcount == 1

    def _transition(
        self,
        session_id: str,
        idempotency_key: str,
        *,
        state: LedgerState,
        result: Any = None,
        note: str | None = None,
    ) -> LedgerEntry:
        result_json = _serialize_result(result) if result is not None else None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE operations
                SET state=?, result_json=?, reconciliation_note=?, updated_at=CURRENT_TIMESTAMP
                WHERE session_id=? AND idempotency_key=?
                """,
                (state.value, result_json, note, session_id, idempotency_key),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise MspError(ErrorCode.INTERNAL_ERROR, "Ledger entry does not exist")
            row = connection.execute(
                "SELECT * FROM operations WHERE session_id=? AND idempotency_key=?",
                (session_id, idempotency_key),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._entry(row)

    def mark_committed(self, session_id: str, idempotency_key: str, result: Any) -> LedgerEntry:
        return self._transition(
            session_id,
            idempotency_key,
            state=LedgerState.COMMITTED_RECEIPT,
            result=result,
        )

    def mark_unknown(self, session_id: str, idempotency_key: str, note: str) -> LedgerEntry:
        return self._transition(
            session_id,
            idempotency_key,
            state=LedgerState.UNKNOWN_COMMIT_STATE,
            note=note,
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
            note=note,
        )

    def store_plan(self, record: PlanRecord) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO plans(
                    token, plan_id, fingerprint, session_id, project_key, state_token,
                    atomicity, expires_at, consumed
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.token,
                    record.plan_id,
                    record.fingerprint,
                    record.project.session_id,
                    record.project.project_key,
                    record.state_before.token,
                    record.atomicity.value,
                    record.expires_at.isoformat(),
                    int(record.consumed),
                ),
            )
            connection.commit()

    def consume_plan(self, token: str, fingerprint: str, now: datetime) -> PlanRecord:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM plans WHERE token=?", (token,)).fetchone()
            if row is None or row["fingerprint"] != fingerprint or bool(row["consumed"]):
                connection.rollback()
                raise MspError(ErrorCode.CONFIRMATION_MISMATCH, "Confirmation token does not match an active plan")
            expires_at = datetime.fromisoformat(row["expires_at"])
            if now >= expires_at:
                connection.rollback()
                raise MspError(ErrorCode.CONFIRMATION_EXPIRED, "Confirmation plan has expired")
            cursor = connection.execute(
                "UPDATE plans SET consumed=1, consumed_at=? WHERE token=? AND consumed=0",
                (now.isoformat(), token),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise MspError(ErrorCode.CONFIRMATION_MISMATCH, "Confirmation token was already consumed")
            connection.commit()
        return PlanRecord(
            plan_id=row["plan_id"],
            token=row["token"],
            fingerprint=row["fingerprint"],
            project=ProjectRef(session_id=row["session_id"], project_key=row["project_key"]),
            state_before=ProjectState(token=row["state_token"]),
            atomicity=Atomicity(row["atomicity"]),
            expires_at=expires_at,
            consumed=True,
        )

    def close(self) -> None:
        return None
