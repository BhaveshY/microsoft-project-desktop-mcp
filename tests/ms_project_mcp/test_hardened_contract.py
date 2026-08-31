from __future__ import annotations

import builtins
import importlib
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from pydantic import ValidationError

from ms_project_mcp.errors import ErrorCode, MspError
from ms_project_mcp.factory import create_backend
from ms_project_mcp.ledger import InMemoryOperationLedger, LedgerEntry, LedgerState
from ms_project_mcp.mock import MockProjectBackend
from ms_project_mcp.models import (
    AddDependency,
    ApplyRequest,
    Atomicity,
    BatchMode,
    CalendarException,
    ClearBaseline,
    CloseDisposition,
    CreateAssignment,
    CreateCalendar,
    CreateResource,
    CreateTask,
    DeleteAssignment,
    DeleteCalendar,
    DeleteResource,
    DeleteTask,
    DesktopProjectDetection,
    ExportRequest,
    MoveTask,
    ObjectKind,
    ObjectRef,
    OperationBatch,
    ProjectAction,
    ProjectRequest,
    QueryEntity,
    QueryRequest,
    ResourceType,
    ScheduleCommand,
    ScheduleRequest,
    SetBaseline,
    SetStatusDate,
    StatusRequest,
    TaskProgressUpdate,
    UpdateAssignment,
    UpdateCalendar,
    UpdateProjectProperties,
    UpdateResource,
    UpdateTask,
    Weekday,
    WorkingDay,
    WorkingInterval,
)
from ms_project_mcp.service import ProjectService
from ms_project_mcp.unavailable import UnavailableProjectBackend


class HardenedContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = MockProjectBackend()
        self.service = ProjectService(self.backend, confirmation_secret=b"hardened-contract-secret")

    def _create(self):
        return self.service.project(ProjectRequest(action=ProjectAction.CREATE, name="Program", idempotency_key="create-program-0001"))

    def _seed_tasks(self, count: int = 2, *, key: str = "seed-hardened-0001"):
        session = self._create()
        receipt = self.service.apply(
            ApplyRequest(
                project=session.project,
                batch=OperationBatch(
                    operations=tuple(
                        CreateTask(client_ref=f"task-{index}", name=f"Task {index}")
                        for index in range(1, count + 1)
                    ),
                    expected_state=session.state,
                    idempotency_key=key,
                    mode=BatchMode.COMMIT,
                ),
            )
        )
        return session, receipt

    def test_typed_lifecycle_domain_round_trip(self) -> None:
        session = self._create()
        weekday = WorkingDay(
            weekday=Weekday.MONDAY,
            intervals=(
                WorkingInterval(start=time(8), end=time(12)),
                WorkingInterval(start=time(13), end=time(17)),
            ),
        )
        holiday = CalendarException(
            name="Launch holiday",
            start_date=date(2027, 1, 1),
            end_date=date(2027, 1, 1),
        )
        operations = (
            CreateTask(client_ref="root", name="Launch", duration_minutes=960, fixed_cost=Decimal("100")),
            CreateTask(
                client_ref="child",
                name="Build",
                duration_minutes=480,
                parent=ObjectRef(kind=ObjectKind.TASK, client_ref="root"),
            ),
            CreateResource(
                client_ref="engineer",
                name="Engineer",
                resource_type=ResourceType.WORK,
                standard_rate=Decimal("125.50"),
                overtime_rate_per_hour=Decimal("188.25"),
                cost_per_use=Decimal("10"),
            ),
            CreateAssignment(
                client_ref="build-owner",
                task=ObjectRef(kind=ObjectKind.TASK, client_ref="child"),
                resource=ObjectRef(kind=ObjectKind.RESOURCE, client_ref="engineer"),
                units_percent=Decimal("80"),
                work_minutes=480,
            ),
            AddDependency(
                predecessor=ObjectRef(kind=ObjectKind.TASK, client_ref="root"),
                successor=ObjectRef(kind=ObjectKind.TASK, client_ref="child"),
                dependency_type="SS",
                lag_minutes=120,
            ),
            CreateCalendar(
                client_ref="launch-calendar",
                name="Launch Calendar",
                weekly=(weekday,),
                exceptions=(holiday,),
            ),
            UpdateProjectProperties(
                title="Launch Program",
                manager="Ada",
                project_start=datetime(2027, 1, 4, 8, 0),
            ),
            SetBaseline(baseline=0),
        )
        plan_request = ApplyRequest(
            project=session.project,
            batch=OperationBatch(
                operations=operations,
                expected_state=session.state,
                idempotency_key="domain-create-0001",
                mode=BatchMode.PLAN,
            ),
        )
        plan = self.service.apply(plan_request)
        self.assertTrue(plan.confirmation_required)
        self.assertEqual(plan.atomicity, Atomicity.UNDO_ATOMIC)
        receipt = self.service.apply(
            ApplyRequest(
                project=session.project,
                batch=plan_request.batch.model_copy(
                    update={"mode": BatchMode.COMMIT, "confirmation_token": plan.confirmation_token}
                ),
            )
        )
        self.assertEqual(receipt.impact["task_count"], 2)
        self.assertEqual(receipt.impact["resource_count"], 1)
        self.assertEqual(receipt.impact["assignment_count"], 1)
        self.assertEqual(receipt.impact["calendar_count"], 1)

        task_page = self.service.query(QueryRequest(project=session.project, entity=QueryEntity.TASK))
        resource_page = self.service.query(QueryRequest(project=session.project, entity=QueryEntity.RESOURCE))
        assignment_page = self.service.query(QueryRequest(project=session.project, entity=QueryEntity.ASSIGNMENT))
        calendar_page = self.service.query(QueryRequest(project=session.project, entity=QueryEntity.CALENDAR))
        baseline_page = self.service.query(QueryRequest(project=session.project, entity=QueryEntity.BASELINE))
        self.assertEqual(task_page.items[1]["parent_id"], 1)
        self.assertEqual(resource_page.items[0]["standard_rate"], "125.50")
        self.assertEqual(assignment_page.items[0]["task_id"], 2)
        self.assertEqual(calendar_page.items[0]["weekly"][0]["weekday"], "monday")
        self.assertEqual(baseline_page.items, ({"baseline": 0, "set": True},))

        status = self.service.status(
            StatusRequest(
                project=session.project,
                expected_state=receipt.state_after,
                idempotency_key="domain-status-0001",
                mode=BatchMode.COMMIT,
                updates=(
                    SetStatusDate(status_date=datetime(2027, 1, 5, 17, 0)),
                    TaskProgressUpdate(
                        task=ObjectRef(kind=ObjectKind.TASK, unique_id=2),
                        percent_complete=Decimal("50"),
                        actual_work_minutes=240,
                        remaining_work_minutes=240,
                    ),
                ),
            )
        )
        state_after_status = self.backend.current_state(session.project)
        self.assertEqual(status["result"]["updated"], 2)

        cleanup_operations = (
            UpdateTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=1), fixed_cost=Decimal("150")),
            MoveTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=2), to_root=True),
            UpdateResource(
                resource=ObjectRef(kind=ObjectKind.RESOURCE, unique_id=1),
                standard_rate=Decimal("130"),
            ),
            UpdateAssignment(
                assignment=ObjectRef(kind=ObjectKind.ASSIGNMENT, unique_id=1),
                units_percent=Decimal("100"),
            ),
            DeleteAssignment(assignment=ObjectRef(kind=ObjectKind.ASSIGNMENT, unique_id=1)),
            DeleteResource(resource=ObjectRef(kind=ObjectKind.RESOURCE, unique_id=1)),
            UpdateCalendar(calendar=ObjectRef(kind=ObjectKind.CALENDAR, unique_id=1), name="Revised Calendar"),
            DeleteCalendar(calendar=ObjectRef(kind=ObjectKind.CALENDAR, unique_id=1)),
            ClearBaseline(baseline=0),
        )
        cleanup_plan_request = ApplyRequest(
            project=session.project,
            batch=OperationBatch(
                operations=cleanup_operations,
                expected_state=state_after_status,
                idempotency_key="domain-cleanup-0001",
                mode=BatchMode.PLAN,
            ),
        )
        cleanup_plan = self.service.apply(cleanup_plan_request)
        cleanup = self.service.apply(
            ApplyRequest(
                project=session.project,
                batch=cleanup_plan_request.batch.model_copy(
                    update={"mode": BatchMode.COMMIT, "confirmation_token": cleanup_plan.confirmation_token}
                ),
            )
        )
        self.assertEqual(cleanup.impact["resource_count"], 0)
        self.assertEqual(cleanup.impact["assignment_count"], 0)
        self.assertEqual(cleanup.impact["calendar_count"], 0)
        tasks = self.service.query(QueryRequest(project=session.project, entity=QueryEntity.TASK))
        self.assertIsNone(tasks.items[1]["parent_id"])

    def test_progress_is_not_a_planning_field_and_datetimes_are_project_local(self) -> None:
        with self.assertRaises(ValidationError):
            UpdateTask(
                task=ObjectRef(kind=ObjectKind.TASK, unique_id=1),
                percent_complete=50,
            )
        aware = datetime(2027, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(ValidationError):
            TaskProgressUpdate(
                task=ObjectRef(kind=ObjectKind.TASK, unique_id=1),
                actual_start=aware,
            )
        with self.assertRaises(ValidationError):
            UpdateProjectProperties(project_start=aware)
        with self.assertRaises(ValidationError):
            SetStatusDate(status_date=aware)

    def test_cross_operation_references_must_target_an_earlier_create(self) -> None:
        session = self._create()
        invalid = OperationBatch(
            operations=(
                CreateAssignment(
                    client_ref="assignment",
                    task=ObjectRef(kind=ObjectKind.TASK, client_ref="later-task"),
                    resource=ObjectRef(kind=ObjectKind.RESOURCE, client_ref="later-resource"),
                ),
                CreateTask(client_ref="later-task", name="Later"),
                CreateResource(client_ref="later-resource", name="Later resource"),
            ),
            expected_state=session.state,
            idempotency_key="ordering-test-0001",
            mode=BatchMode.COMMIT,
        )
        with self.assertRaises(MspError) as raised:
            self.service.apply(ApplyRequest(project=session.project, batch=invalid))
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)

    def test_confirmation_requires_stored_unexpired_plan_and_exact_payload(self) -> None:
        now = [datetime(2027, 1, 1, tzinfo=timezone.utc)]
        backend = MockProjectBackend()
        service = ProjectService(
            backend,
            confirmation_secret=b"expiring-plan-secret",
            clock=lambda: now[0],
        )
        session = service.project(ProjectRequest(action=ProjectAction.CREATE, name="Expiry", idempotency_key="create-expiry-0001"))
        seeded = service.apply(
            ApplyRequest(
                project=session.project,
                batch=OperationBatch(
                    operations=(
                        CreateTask(client_ref="one", name="One"),
                        CreateTask(client_ref="two", name="Two"),
                    ),
                    expected_state=session.state,
                    idempotency_key="expiry-seed-0001",
                    mode=BatchMode.COMMIT,
                ),
            )
        )
        delete_two = OperationBatch(
            operations=(DeleteTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=2)),),
            expected_state=seeded.state_after,
            idempotency_key="expiry-delete-0001",
            mode=BatchMode.PLAN,
        )
        plan = service.apply(ApplyRequest(project=session.project, batch=delete_two))

        with self.assertRaises(MspError) as drift:
            service.apply(
                ApplyRequest(
                    project=session.project,
                    batch=delete_two.model_copy(
                        update={
                            "mode": BatchMode.COMMIT,
                            "operations": (DeleteTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=1)),),
                            "confirmation_token": plan.confirmation_token,
                        }
                    ),
                )
            )
        self.assertEqual(drift.exception.code, ErrorCode.CONFIRMATION_MISMATCH)

        now[0] += timedelta(minutes=11)
        with self.assertRaises(MspError) as expired:
            service.apply(
                ApplyRequest(
                    project=session.project,
                    batch=delete_two.model_copy(
                        update={"mode": BatchMode.COMMIT, "confirmation_token": plan.confirmation_token}
                    ),
                )
            )
        self.assertEqual(expired.exception.code, ErrorCode.CONFIRMATION_EXPIRED)

        with self.assertRaises(MspError) as invented:
            service.apply(
                ApplyRequest(
                    project=session.project,
                    batch=delete_two.model_copy(
                        update={"mode": BatchMode.COMMIT, "confirmation_token": "invented"}
                    ),
                )
            )
        self.assertEqual(invented.exception.code, ErrorCode.CONFIRMATION_MISMATCH)

    def test_atomicity_is_family_specific(self) -> None:
        session, receipt = self._seed_tasks(1)
        schedule_plan = self.service.schedule(
            ScheduleRequest(
                project=session.project,
                command=ScheduleCommand.LEVEL,
                expected_state=receipt.state_after,
                idempotency_key="atomic-schedule-0001",
                mode=BatchMode.PLAN,
            )
        )
        export_plan = self.service.export(
            ExportRequest(
                project=session.project,
                format="pdf",
                destination="C:\\Reports\\plan.pdf",
                expected_state=receipt.state_after,
                idempotency_key="atomic-export-0001",
                mode=BatchMode.PLAN,
            )
        )
        self.assertEqual(schedule_plan.atomicity, Atomicity.CHECKPOINTED)
        self.assertEqual(export_plan.atomicity, Atomicity.NON_ATOMIC)

    def test_ledger_rejects_idempotency_collisions_across_action_families(self) -> None:
        session = self._create()
        key = "global-key-0001"
        receipt = self.service.apply(
            ApplyRequest(
                project=session.project,
                batch=OperationBatch(
                    operations=(CreateTask(client_ref="task", name="Task"),),
                    expected_state=session.state,
                    idempotency_key=key,
                    mode=BatchMode.COMMIT,
                ),
            )
        )
        requests = (
            lambda: self.service.schedule(
                ScheduleRequest(
                    project=session.project,
                    command=ScheduleCommand.CALCULATE,
                    expected_state=receipt.state_after,
                    idempotency_key=key,
                    mode=BatchMode.COMMIT,
                )
            ),
            lambda: self.service.status(
                StatusRequest(
                    project=session.project,
                    expected_state=receipt.state_after,
                    idempotency_key=key,
                    mode=BatchMode.COMMIT,
                    updates=(
                        TaskProgressUpdate(
                            task=ObjectRef(kind=ObjectKind.TASK, unique_id=1),
                            percent_complete=Decimal("10"),
                        ),
                    ),
                )
            ),
            lambda: self.service.export(
                ExportRequest(
                    project=session.project,
                    format="csv",
                    destination="C:\\Reports\\plan.csv",
                    expected_state=receipt.state_after,
                    idempotency_key=key,
                    mode=BatchMode.COMMIT,
                )
            ),
            lambda: self.service.project(
                ProjectRequest(
                    action=ProjectAction.SAVE,
                    project=session.project,
                    expected_state=receipt.state_after,
                    idempotency_key=key,
                )
            ),
            lambda: self.service.project(
                ProjectRequest(
                    action=ProjectAction.CLOSE,
                    project=session.project,
                    expected_state=receipt.state_after,
                    idempotency_key=key,
                    close_disposition=CloseDisposition.REFUSE_IF_DIRTY,
                )
            ),
        )
        for action in requests:
            with self.subTest(action=action):
                with self.assertRaises(MspError) as raised:
                    action()
                self.assertEqual(raised.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)

    def test_ledger_state_machine_exposes_unknown_and_reconciliation(self) -> None:
        ledger = InMemoryOperationLedger()
        entry = ledger.begin(
            LedgerEntry(
                session_id="session",
                idempotency_key="ledger-key",
                request_family="apply",
                fingerprint="fingerprint",
                state=LedgerState.PENDING_DISPATCH,
            )
        )
        self.assertEqual(entry.state, LedgerState.PENDING_DISPATCH)
        unknown = ledger.mark_unknown("session", "ledger-key", "transport_lost")
        self.assertEqual(unknown.state, LedgerState.UNKNOWN_COMMIT_STATE)
        reconciling = ledger.begin_reconciliation("session", "ledger-key")
        self.assertEqual(reconciling.state, LedgerState.RECONCILIATION)
        committed = ledger.complete_reconciliation(
            "session",
            "ledger-key",
            committed=True,
            result={"receipt": "reconciled"},
        )
        self.assertEqual(committed.state, LedgerState.COMMITTED_RECEIPT)

    def test_dispatch_failure_becomes_unknown_and_is_not_replayed(self) -> None:
        class FailingBackend(MockProjectBackend):
            def schedule(self, project, command, options, *, expected_state):
                raise RuntimeError("transport lost after dispatch")

        backend = FailingBackend()
        service = ProjectService(backend, confirmation_secret=b"fault-secret")
        session = service.project(ProjectRequest(action=ProjectAction.CREATE, name="Fault", idempotency_key="create-fault-0001"))
        request = ScheduleRequest(
            project=session.project,
            command=ScheduleCommand.CALCULATE,
            expected_state=session.state,
            idempotency_key="fault-schedule-0001",
            mode=BatchMode.COMMIT,
        )
        with self.assertRaises(RuntimeError):
            service.schedule(request)
        entry = service.ledger.lookup(session.project.session_id, request.idempotency_key)
        self.assertEqual(entry.state, LedgerState.UNKNOWN_COMMIT_STATE)
        with self.assertRaises(MspError) as replay:
            service.schedule(request)
        self.assertEqual(replay.exception.code, ErrorCode.UNKNOWN_COMMIT_STATE)

    def test_query_cursor_is_opaque_bound_and_stale_after_mutation(self) -> None:
        session, receipt = self._seed_tasks(3)
        first = self.service.query(
            QueryRequest(project=session.project, entity=QueryEntity.TASK, limit=1)
        )
        self.assertIsNotNone(first.next_cursor)
        self.assertTrue(first.next_cursor.startswith("v1."))
        self.assertNotEqual(first.next_cursor, "1")
        with self.assertRaises(MspError) as projection:
            self.service.query(
                QueryRequest(
                    project=session.project,
                    entity=QueryEntity.TASK,
                    fields=("name",),
                    limit=1,
                    cursor=first.next_cursor,
                )
            )
        self.assertEqual(projection.exception.code, ErrorCode.INVALID_REQUEST)

        self.service.apply(
            ApplyRequest(
                project=session.project,
                batch=OperationBatch(
                    operations=(CreateTask(client_ref="new", name="New"),),
                    expected_state=receipt.state_after,
                    idempotency_key="cursor-mutate-0001",
                    mode=BatchMode.COMMIT,
                ),
            )
        )
        with self.assertRaises(MspError) as stale:
            self.service.query(
                QueryRequest(
                    project=session.project,
                    entity=QueryEntity.TASK,
                    limit=1,
                    cursor=first.next_cursor,
                )
            )
        self.assertEqual(stale.exception.code, ErrorCode.STALE_STATE)

    def test_auto_backend_is_unavailable_and_mock_requires_explicit_opt_in(self) -> None:
        detection = DesktopProjectDetection(
            platform="Test",
            windows=False,
            com_registered=False,
            pywin32_importable=False,
            pythoncom_importable=False,
            win32com_importable=False,
        )
        unavailable = create_backend({}, detection=detection)
        self.assertIsInstance(unavailable, UnavailableProjectBackend)
        capabilities = unavailable.capabilities()
        self.assertFalse(capabilities.available)
        self.assertFalse(capabilities.activates_desktop)
        with self.assertRaises(MspError) as raised:
            unavailable.create_project(name="No desktop", path=None)
        self.assertEqual(raised.exception.code, ErrorCode.BACKEND_UNAVAILABLE)
        self.assertIsInstance(create_backend({"MSP_MCP_BACKEND": "mock"}), MockProjectBackend)

        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "pythoncom" or name.startswith("win32com"):
                raise AssertionError("Server import attempted to activate the Windows COM stack")
            return original_import(name, *args, **kwargs)

        import ms_project_mcp.server as server_module

        with tempfile.TemporaryDirectory() as state_dir:
            with patch.dict(
                "os.environ",
                {"MSP_MCP_BACKEND": "auto", "MSP_MCP_STATE_DIR": state_dir},
            ):
                with patch("ms_project_mcp.factory.probe_desktop_project", return_value=detection):
                    with patch("builtins.__import__", side_effect=guarded_import):
                        reloaded = importlib.reload(server_module)
                    result = reloaded.msp_capabilities()
        self.assertFalse(result["result"]["available"])


if __name__ == "__main__":
    unittest.main()
