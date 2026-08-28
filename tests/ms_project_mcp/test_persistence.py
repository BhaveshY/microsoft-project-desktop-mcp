from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from ms_project_mcp.errors import ErrorCode, MspError
from ms_project_mcp.factory import create_service
from ms_project_mcp.ledger import LedgerEntry, LedgerState, PlanRecord
from ms_project_mcp.mock import MockProjectBackend
from ms_project_mcp.models import (
    ApplyRequest,
    Atomicity,
    BatchMode,
    ChangeReceipt,
    CommitState,
    CreateTask,
    ObjectKind,
    ObjectRef,
    OperationBatch,
    Ownership,
    ProjectAction,
    ProjectRef,
    ProjectRequest,
    ProjectSession,
    ProjectState,
    QueryEntity,
    QueryRequest,
    SetBaseline,
    VerificationLevel,
)
from ms_project_mcp.persistence import SECRET_FILE, load_or_create_secret, resolve_state_dir
from ms_project_mcp.service import ProjectService
from ms_project_mcp.sqlite_ledger import (
    ORPHAN_NOTE,
    SCHEMA_VERSION,
    SQLiteOperationLedger,
    _process_alive,
    _windows_process_alive,
)


def _claim_in_process(path: str, ready, start, results) -> None:
    ledger = SQLiteOperationLedger(path)
    ready.put(True)
    start.wait(10)
    entry = LedgerEntry(
        session_id="process-session",
        idempotency_key="process-key",
        request_family="apply",
        fingerprint="process-fingerprint",
        state=LedgerState.PENDING_DISPATCH,
    )
    results.put(ledger.claim_dispatch(entry).acquired)


class PersistentStateTests(unittest.TestCase):
    def test_state_root_prefers_override_then_windows_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            override = Path(directory) / "override"
            self.assertEqual(resolve_state_dir({"MSP_MCP_STATE_DIR": str(override)}), override.resolve())
            local = Path(directory) / "LocalAppData"
            self.assertEqual(
                resolve_state_dir({"LOCALAPPDATA": str(local)}),
                (local / "OpenAI" / "MicrosoftProjectMCP").resolve(),
            )

    def test_secret_is_atomic_persistent_and_acl_failure_is_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            acl_calls: list[Path] = []

            def failing_acl(path: Path) -> None:
                acl_calls.append(path)
                raise RuntimeError("icacls unavailable")

            first = load_or_create_secret(root, random_bytes=lambda size: b"a" * size, acl_applier=failing_acl)
            second = load_or_create_secret(root, random_bytes=lambda size: b"b" * size, acl_applier=failing_acl)
            self.assertEqual(first, b"a" * 32)
            self.assertEqual(second, first)
            self.assertTrue((root / SECRET_FILE).is_file())
            self.assertGreaterEqual(len(acl_calls), 2)

    def test_concurrent_secret_creators_observe_one_complete_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            barrier = Barrier(5)

            def create(index: int) -> bytes:
                barrier.wait(timeout=5)
                return load_or_create_secret(
                    root,
                    random_bytes=lambda size: bytes([index]) * size,
                    acl_applier=lambda path: None,
                )

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(create, index) for index in range(1, 5)]
                barrier.wait(timeout=5)
                values = [future.result(timeout=5) for future in futures]
            self.assertEqual(len(set(values)), 1)
            self.assertEqual(len(values[0]), 32)


class SQLiteOperationLedgerTests(unittest.TestCase):
    def _path(self, directory: str) -> Path:
        return Path(directory) / "ledger.sqlite3"

    @staticmethod
    def _entry(*, fingerprint: str = "fingerprint", family: str = "apply") -> LedgerEntry:
        return LedgerEntry(
            session_id="session",
            idempotency_key="idempotency-key",
            request_family=family,
            fingerprint=fingerprint,
            state=LedgerState.PENDING_DISPATCH,
        )

    def test_schema_version_wal_and_json_typed_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            ledger = SQLiteOperationLedger(path)
            ledger.claim_dispatch(self._entry())
            project = ProjectRef(session_id="session", project_key="project")
            state = ProjectState(token="sha256:" + "1" * 64)
            receipt = ChangeReceipt(
                receipt_id="receipt",
                project=project,
                idempotency_key="idempotency-key",
                state_before=state,
                state_after=ProjectState(token="sha256:" + "2" * 64),
                requested=({"op": "set_baseline"},),
                observed=({"baseline": 0},),
                verification=VerificationLevel.NATIVE_REREAD,
                atomicity=Atomicity.UNDO_ATOMIC,
                undo_available=True,
                commit_state=CommitState.COMMITTED,
            )
            value = {
                "receipt": receipt,
                "session": ProjectSession(
                    project=project,
                    ownership=Ownership.SERVER_OWNED,
                    name="Plan",
                    dirty=False,
                    state=state,
                ),
                "ref": ObjectRef(kind=ObjectKind.TASK, unique_id=7),
                "tuple": (Decimal("12.50"), datetime(2028, 1, 1, tzinfo=timezone.utc)),
            }
            ledger.mark_committed("session", "idempotency-key", value)

            reopened = SQLiteOperationLedger(path)
            result = reopened.lookup("session", "idempotency-key").result
            self.assertIsInstance(result["receipt"], ChangeReceipt)
            self.assertIsInstance(result["session"], ProjectSession)
            self.assertIsInstance(result["ref"], ObjectRef)
            self.assertEqual(result["tuple"][0], Decimal("12.50"))
            self.assertEqual(result["tuple"][1].tzinfo, timezone.utc)
            connection = sqlite3.connect(path)
            try:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                payload = connection.execute("SELECT result_json FROM operations").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(int(version), SCHEMA_VERSION)
            self.assertEqual(mode.lower(), "wal")
            self.assertIsInstance(json.loads(payload), dict)

    def test_claim_is_atomic_across_independent_connections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            ledgers = [SQLiteOperationLedger(path) for _ in range(4)]
            barrier = Barrier(5)

            def claim(ledger):
                barrier.wait(timeout=5)
                return ledger.claim_dispatch(self._entry()).acquired

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(claim, ledger) for ledger in ledgers]
                barrier.wait(timeout=5)
                acquired = [future.result(timeout=10) for future in futures]
            self.assertEqual(acquired.count(True), 1)
            with self.assertRaises(MspError) as conflict:
                ledgers[0].claim_dispatch(self._entry(fingerprint="different", family="schedule"))
            self.assertEqual(conflict.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)

    def test_claim_is_atomic_across_spawned_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("spawn")
            ready = context.Queue()
            results = context.Queue()
            start = context.Event()
            path = str(self._path(directory))
            workers = [
                context.Process(target=_claim_in_process, args=(path, ready, start, results))
                for _ in range(2)
            ]
            for worker in workers:
                worker.start()
            self.assertTrue(ready.get(timeout=15))
            self.assertTrue(ready.get(timeout=15))
            start.set()
            acquired = [results.get(timeout=15), results.get(timeout=15)]
            for worker in workers:
                worker.join(timeout=15)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(acquired.count(True), 1)

    def test_startup_marks_only_dead_owner_pending_dispatch_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            crashed = SQLiteOperationLedger(path, owner_pid=111, process_alive=lambda pid: True)
            crashed.claim_dispatch(self._entry())
            active_view = SQLiteOperationLedger(path, owner_pid=222, process_alive=lambda pid: True)
            self.assertEqual(
                active_view.lookup("session", "idempotency-key").state,
                LedgerState.PENDING_DISPATCH,
            )
            recovered = SQLiteOperationLedger(path, owner_pid=333, process_alive=lambda pid: pid != 111)
            entry = recovered.lookup("session", "idempotency-key")
            self.assertEqual(entry.state, LedgerState.UNKNOWN_COMMIT_STATE)
            self.assertEqual(entry.reconciliation_note, ORPHAN_NOTE)

    def test_default_windows_liveness_probe_never_calls_os_kill(self) -> None:
        with (
            patch("ms_project_mcp.sqlite_ledger.os.name", "nt"),
            patch("ms_project_mcp.sqlite_ledger.os.getpid", return_value=999),
            patch("ms_project_mcp.sqlite_ledger._windows_process_alive", return_value=True) as probe,
            patch("ms_project_mcp.sqlite_ledger.os.kill") as kill,
        ):
            self.assertTrue(_process_alive(123))
        probe.assert_called_once_with(123)
        kill.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows process handles only")
    def test_windows_liveness_probe_reads_current_process_handle(self) -> None:
        self.assertTrue(_windows_process_alive(os.getpid()))

    def test_plan_reopens_expires_and_is_consumed_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            now = datetime(2028, 1, 1, tzinfo=timezone.utc)
            project = ProjectRef(session_id="session", project_key="project")
            record = PlanRecord(
                plan_id="plan",
                token="token",
                fingerprint="fingerprint",
                project=project,
                state_before=ProjectState(token="sha256:" + "a" * 64),
                atomicity=Atomicity.UNDO_ATOMIC,
                expires_at=now + timedelta(minutes=5),
            )
            SQLiteOperationLedger(path).store_plan(record)
            reopened = SQLiteOperationLedger(path)
            consumed = reopened.consume_plan("token", "fingerprint", now)
            self.assertTrue(consumed.consumed)
            with self.assertRaises(MspError) as repeated:
                SQLiteOperationLedger(path).consume_plan("token", "fingerprint", now)
            self.assertEqual(repeated.exception.code, ErrorCode.CONFIRMATION_MISMATCH)

            expired = record.__class__(**{**record.__dict__, "token": "expired", "expires_at": now})
            reopened.store_plan(expired)
            with self.assertRaises(MspError) as expiry:
                reopened.consume_plan("expired", "fingerprint", now)
            self.assertEqual(expiry.exception.code, ErrorCode.CONFIRMATION_EXPIRED)

    def test_plan_consumption_is_atomic_across_independent_connections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            now = datetime(2028, 1, 1, tzinfo=timezone.utc)
            record = PlanRecord(
                plan_id="concurrent-plan",
                token="concurrent-token",
                fingerprint="concurrent-fingerprint",
                project=ProjectRef(session_id="session", project_key="project"),
                state_before=ProjectState(token="sha256:" + "b" * 64),
                atomicity=Atomicity.UNDO_ATOMIC,
                expires_at=now + timedelta(minutes=5),
            )
            SQLiteOperationLedger(path).store_plan(record)
            ledgers = [SQLiteOperationLedger(path), SQLiteOperationLedger(path)]
            barrier = Barrier(3)

            def consume(ledger: SQLiteOperationLedger) -> str:
                barrier.wait(timeout=5)
                try:
                    ledger.consume_plan(record.token, record.fingerprint, now)
                except MspError as error:
                    return error.code.value
                return "consumed"

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(consume, ledger) for ledger in ledgers]
                barrier.wait(timeout=5)
                outcomes = [future.result(timeout=10) for future in futures]
            self.assertEqual(outcomes.count("consumed"), 1)
            self.assertEqual(outcomes.count(ErrorCode.CONFIRMATION_MISMATCH.value), 1)

    def test_unknown_and_reconciliation_states_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            ledger = SQLiteOperationLedger(path)
            ledger.claim_dispatch(self._entry())
            ledger.mark_unknown("session", "idempotency-key", "outcome uncertain")
            reopened = SQLiteOperationLedger(path)
            unknown = reopened.lookup("session", "idempotency-key")
            self.assertEqual(unknown.state, LedgerState.UNKNOWN_COMMIT_STATE)
            self.assertEqual(unknown.reconciliation_note, "outcome uncertain")
            reopened.begin_reconciliation("session", "idempotency-key")
            reconciling = SQLiteOperationLedger(path).lookup("session", "idempotency-key")
            self.assertEqual(reconciling.state, LedgerState.RECONCILIATION)
            reopened.complete_reconciliation(
                "session",
                "idempotency-key",
                committed=False,
                note="manual verification required",
            )
            final = SQLiteOperationLedger(path).lookup("session", "idempotency-key")
            self.assertEqual(final.state, LedgerState.UNKNOWN_COMMIT_STATE)
            self.assertEqual(final.reconciliation_note, "manual verification required")

    def test_database_sidecars_receive_best_effort_user_acl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            protected: list[str] = []

            def record(path: Path) -> None:
                protected.append(path.name)
                raise RuntimeError("ACL tooling unavailable")

            ledger = SQLiteOperationLedger(self._path(directory), acl_applier=record)
            ledger.claim_dispatch(self._entry())
            self.assertIn("ledger.sqlite3", protected)
            self.assertTrue(any(name.endswith("-wal") for name in protected))
            self.assertTrue(any(name.endswith("-shm") for name in protected))


class PersistentServiceTests(unittest.TestCase):
    def test_backend_namespaces_prevent_cross_process_ledger_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite3"
            first_backend = MockProjectBackend(instance_namespace="process-a")
            second_backend = MockProjectBackend(instance_namespace="process-b")
            first = ProjectService(first_backend, ledger=SQLiteOperationLedger(path))
            second = ProjectService(second_backend, ledger=SQLiteOperationLedger(path))
            first_session = first.project(ProjectRequest(action=ProjectAction.CREATE, name="Plan", idempotency_key="create-plan-first-0001"))
            second_session = second.project(ProjectRequest(action=ProjectAction.CREATE, name="Plan", idempotency_key="create-plan-second-0001"))
            self.assertNotEqual(first_session.project, second_session.project)

            def request(session: ProjectSession) -> ApplyRequest:
                return ApplyRequest(
                    project=session.project,
                    batch=OperationBatch(
                        operations=(CreateTask(client_ref="task", name="Task"),),
                        expected_state=session.state,
                        idempotency_key="same-key-0001",
                        mode=BatchMode.COMMIT,
                    ),
                )

            first_receipt = first.apply(request(first_session))
            second_receipt = second.apply(request(second_session))
            self.assertFalse(first_receipt.replayed)
            self.assertFalse(second_receipt.replayed)
            connection = sqlite3.connect(path)
            try:
                rows = connection.execute(
                    "SELECT session_id FROM operations WHERE idempotency_key='same-key-0001'"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual({row[0] for row in rows}, {
                first_session.project.session_id,
                second_session.project.session_id,
            })
            first.shutdown()
            second.shutdown()
    def test_apply_confirmation_and_typed_receipt_replay_survive_service_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite3"
            secret = b"persistent-service-secret-0001"[:32].ljust(32, b"x")
            backend = MockProjectBackend()
            first = ProjectService(backend, ledger=SQLiteOperationLedger(path), confirmation_secret=secret)
            session = first.project(ProjectRequest(action=ProjectAction.CREATE, name="Persistent", idempotency_key="create-persistent-0001"))
            plan_request = ApplyRequest(
                project=session.project,
                batch=OperationBatch(
                    operations=(SetBaseline(baseline=0),),
                    expected_state=session.state,
                    idempotency_key="persistent-apply-0001",
                    mode=BatchMode.PLAN,
                ),
            )
            plan = first.apply(plan_request)
            commit = plan_request.model_copy(
                update={
                    "batch": plan_request.batch.model_copy(
                        update={"mode": BatchMode.COMMIT, "confirmation_token": plan.confirmation_token}
                    )
                }
            )
            second = ProjectService(backend, ledger=SQLiteOperationLedger(path), confirmation_secret=secret)
            receipt = second.apply(commit)
            self.assertIsInstance(receipt, ChangeReceipt)
            third = ProjectService(backend, ledger=SQLiteOperationLedger(path), confirmation_secret=secret)
            replay = third.apply(commit)
            self.assertIsInstance(replay, ChangeReceipt)
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.receipt_id, receipt.receipt_id)

    def test_persistent_secret_keeps_query_cursors_valid_after_service_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = load_or_create_secret(root, acl_applier=lambda path: None)
            backend = MockProjectBackend()
            first = ProjectService(
                backend,
                ledger=SQLiteOperationLedger(root / "ledger.sqlite3"),
                confirmation_secret=secret,
            )
            session = first.project(ProjectRequest(action=ProjectAction.CREATE, name="Cursor", idempotency_key="create-cursor-0001"))
            receipt = first.apply(
                ApplyRequest(
                    project=session.project,
                    batch=OperationBatch(
                        operations=(
                            CreateTask(client_ref="one", name="One"),
                            CreateTask(client_ref="two", name="Two"),
                        ),
                        expected_state=session.state,
                        idempotency_key="cursor-seed-0001",
                        mode=BatchMode.COMMIT,
                    ),
                )
            )
            page = first.query(
                QueryRequest(project=session.project, entity=QueryEntity.TASK, limit=1)
            )
            self.assertIsNotNone(page.next_cursor)
            reopened_secret = load_or_create_secret(root, acl_applier=lambda path: None)
            second = ProjectService(
                backend,
                ledger=SQLiteOperationLedger(root / "ledger.sqlite3"),
                confirmation_secret=reopened_secret,
            )
            next_page = second.query(
                QueryRequest(
                    project=session.project,
                    entity=QueryEntity.TASK,
                    limit=1,
                    cursor=page.next_cursor,
                )
            )
            self.assertEqual(next_page.state, receipt.state_after)
            self.assertEqual(len(next_page.items), 1)

    def test_production_service_factory_uses_sqlite_and_persistent_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "MSP_MCP_STATE_DIR": directory,
                "MSP_MCP_BACKEND": "mock",
            }
            first = create_service(environment)
            second = create_service(environment)
            self.assertIsInstance(first.ledger, SQLiteOperationLedger)
            self.assertIsInstance(second.ledger, SQLiteOperationLedger)
            self.assertEqual(first._secret, second._secret)
            self.assertTrue((Path(directory) / "operation-ledger.sqlite3").is_file())
            self.assertTrue((Path(directory) / SECRET_FILE).is_file())
            first.shutdown()
            second.shutdown()


if __name__ == "__main__":
    unittest.main()
