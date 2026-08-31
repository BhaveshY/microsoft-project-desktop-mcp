from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError

from ms_project_mcp.errors import ErrorCode, MspError
from ms_project_mcp.mock import MockProjectBackend, TOOL_NAMES
from ms_project_mcp.models import (
    AddDependency,
    ApplyRequest,
    BatchMode,
    ChangePlan,
    ChangeReceipt,
    CloseDisposition,
    ContractFidelity,
    CreateCalendar,
    CreateResource,
    CreateTask,
    DeleteTask,
    DesktopSmoke,
    ObjectKind,
    ObjectRef,
    OperationBatch,
    ProjectAction,
    ProjectRequest,
    QueryEntity,
    QueryRequest,
    ResourceType,
    TaskConstraintType,
    TaskType,
    UpdateProjectProperties,
)
from ms_project_mcp.server import mcp
from ms_project_mcp.service import ProjectService


class MicrosoftProjectMcpContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = MockProjectBackend()
        self.service = ProjectService(self.backend, confirmation_secret=b"contract-test-secret")

    def _create(self):
        return self.service.project(ProjectRequest(action=ProjectAction.CREATE, name="Launch Plan", idempotency_key="create-launch-plan-0001"))

    def _seed_two_tasks(self):
        session = self._create()
        batch = OperationBatch(
            operations=(
                CreateTask(client_ref="discovery", name="Discovery", duration_minutes=480),
                CreateTask(client_ref="delivery", name="Delivery", duration_minutes=960),
                AddDependency(
                    predecessor=ObjectRef(kind=ObjectKind.TASK, client_ref="discovery"),
                    successor=ObjectRef(kind=ObjectKind.TASK, client_ref="delivery"),
                ),
            ),
            expected_state=session.state,
            idempotency_key="seed-tasks-0001",
            mode=BatchMode.COMMIT,
        )
        receipt = self.service.apply(ApplyRequest(project=session.project, batch=batch))
        return session, receipt

    def test_server_import_exposes_exactly_eight_locked_tools(self) -> None:
        tools = asyncio.run(mcp.list_tools())
        self.assertEqual(tuple(tool.name for tool in tools), TOOL_NAMES)

    def test_capabilities_are_honest_about_mock_and_desktop_verification(self) -> None:
        capabilities = self.service.capabilities()
        self.assertEqual(capabilities.contract_fidelity, ContractFidelity.CONTRACT_ONLY)
        self.assertEqual(capabilities.scheduling_fidelity, ContractFidelity.CONTRACT_ONLY)
        self.assertEqual(capabilities.desktop_smoke, DesktopSmoke.NOT_VERIFIED)
        self.assertFalse(capabilities.installed)
        self.assertIn("contract_only", " ".join(capabilities.notes))

    def test_project_create_requires_key_and_replays_same_session(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectRequest(action=ProjectAction.CREATE, name="Missing key")
        first = self._create()
        replay = self._create()
        self.assertEqual(replay.project, first.project)
        self.assertEqual(replay.state, first.state)

    def test_full_mock_lifecycle_and_idempotent_replay(self) -> None:
        session = self._create()
        operations = (
            CreateTask(client_ref="discovery", name="Discovery", duration_minutes=480),
            CreateTask(client_ref="delivery", name="Delivery", duration_minutes=960),
            AddDependency(
                predecessor=ObjectRef(kind=ObjectKind.TASK, client_ref="discovery"),
                successor=ObjectRef(kind=ObjectKind.TASK, client_ref="delivery"),
            ),
        )
        plan = self.service.apply(
            ApplyRequest(
                project=session.project,
                batch=OperationBatch(
                    operations=operations,
                    expected_state=session.state,
                    idempotency_key="lifecycle-0001",
                    mode=BatchMode.PLAN,
                ),
            )
        )
        self.assertIsInstance(plan, ChangePlan)
        self.assertFalse(plan.confirmation_required)

        commit_request = ApplyRequest(
            project=session.project,
            batch=OperationBatch(
                operations=operations,
                expected_state=session.state,
                idempotency_key="lifecycle-0001",
                mode=BatchMode.COMMIT,
            ),
        )
        receipt = self.service.apply(commit_request)
        self.assertIsInstance(receipt, ChangeReceipt)
        self.assertFalse(receipt.replayed)
        replay = self.service.apply(commit_request)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.receipt_id, receipt.receipt_id)
        conflicting = ApplyRequest(
            project=session.project,
            batch=commit_request.batch.model_copy(
                update={"operations": (CreateTask(client_ref="other", name="Other"),)}
            ),
        )
        with self.assertRaises(MspError) as conflict:
            self.service.apply(conflicting)
        self.assertEqual(conflict.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)

        task_page = self.service.query(QueryRequest(project=session.project, entity=QueryEntity.TASK))
        dependency_page = self.service.query(
            QueryRequest(project=session.project, entity=QueryEntity.DEPENDENCY)
        )
        self.assertEqual([item["name"] for item in task_page.items], ["Discovery", "Delivery"])
        self.assertEqual(len(dependency_page.items), 1)

        saved = self.service.project(
            ProjectRequest(
                action=ProjectAction.SAVE,
                project=session.project,
                expected_state=receipt.state_after,
                idempotency_key="save-plan-0001",
                path="C:\\Plans\\Launch.mpp",
            )
        )
        self.assertFalse(saved.dirty)
        closed = self.service.project(
            ProjectRequest(
                action=ProjectAction.CLOSE,
                project=session.project,
                expected_state=saved.state,
                idempotency_key="close-plan-0001",
                close_disposition=CloseDisposition.REFUSE_IF_DIRTY,
            )
        )
        self.assertTrue(closed["closed"])

    def test_query_returns_stable_unique_id_references(self) -> None:
        session, _ = self._seed_two_tasks()
        first = self.service.query(QueryRequest(project=session.project, entity=QueryEntity.TASK))
        second = self.service.query(QueryRequest(project=session.project, entity=QueryEntity.TASK))
        self.assertEqual(
            [item["ref"] for item in first.items],
            [item["ref"] for item in second.items],
        )
        self.assertEqual([item["ref"]["unique_id"] for item in first.items], [1, 2])
        with self.assertRaises(ValidationError):
            ObjectRef(kind=ObjectKind.TASK, unique_id=1, client_ref="also-set")

    def test_advanced_standalone_planning_fields_round_trip(self) -> None:
        with self.assertRaises(ValidationError):
            CreateTask(
                client_ref="invalid",
                name="Invalid",
                constraint_type=TaskConstraintType.START_NO_EARLIER_THAN,
            )

        session = self._create()
        start = datetime(2027, 2, 1, 8, 0)
        deadline = datetime(2027, 2, 12, 17, 0)
        operations = (
            CreateCalendar(client_ref="delivery-calendar", name="Delivery Calendar"),
            CreateTask(
                client_ref="controlled-task",
                name="Controlled task",
                duration_minutes=2400,
                constraint_type=TaskConstraintType.START_NO_EARLIER_THAN,
                constraint_date=start,
                deadline=deadline,
                task_type=TaskType.FIXED_DURATION,
                effort_driven=False,
                manual=False,
                priority=700,
                notes="Release gate",
                calendar=ObjectRef(kind=ObjectKind.CALENDAR, client_ref="delivery-calendar"),
                ignore_resource_calendar=True,
            ),
            CreateResource(
                client_ref="lead",
                name="Delivery Lead",
                resource_type=ResourceType.WORK,
                standard_rate=Decimal("150"),
                initials="DL",
                group="Delivery",
                code="DL-01",
                email="lead@example.com",
                notes="Primary owner",
                base_calendar=ObjectRef(kind=ObjectKind.CALENDAR, client_ref="delivery-calendar"),
            ),
            UpdateProjectProperties(
                calendar=ObjectRef(kind=ObjectKind.CALENDAR, client_ref="delivery-calendar"),
                default_task_type=TaskType.FIXED_DURATION,
                default_effort_driven=False,
                new_tasks_manual=False,
                honor_constraints=True,
                multiple_critical_paths=True,
                hours_per_day=Decimal("8"),
                hours_per_week=Decimal("40"),
            ),
        )
        receipt = self.service.apply(
            ApplyRequest(
                project=session.project,
                batch=OperationBatch(
                    operations=operations,
                    expected_state=session.state,
                    idempotency_key="advanced-roundtrip-0001",
                    mode=BatchMode.COMMIT,
                ),
            )
        )
        task = self.service.query(
            QueryRequest(project=session.project, entity=QueryEntity.TASK)
        ).items[0]
        resource = self.service.query(
            QueryRequest(project=session.project, entity=QueryEntity.RESOURCE)
        ).items[0]
        project = self.service.query(
            QueryRequest(project=session.project, entity=QueryEntity.PROJECT)
        ).items[0]
        self.assertEqual(task["constraint_type"], 4)
        self.assertEqual(
            task["constraint_type_name"], TaskConstraintType.START_NO_EARLIER_THAN.value
        )
        self.assertEqual(task["deadline"], deadline.isoformat())
        self.assertEqual(task["task_type"], TaskType.FIXED_DURATION.value)
        self.assertEqual(task["calendar_ref"]["unique_id"], 1)
        self.assertEqual(resource["base_calendar_ref"]["unique_id"], 1)
        self.assertEqual(resource["email"], "lead@example.com")
        self.assertEqual(project["calendar_ref"]["unique_id"], 1)
        self.assertEqual(project["default_task_type"], TaskType.FIXED_DURATION.value)
        self.assertEqual(receipt.state_after, self.backend.current_state(session.project))

    def test_stale_state_is_rejected_before_dispatch(self) -> None:
        session, _ = self._seed_two_tasks()
        stale_batch = OperationBatch(
            operations=(CreateTask(client_ref="late", name="Late task"),),
            expected_state=session.state,
            idempotency_key="stale-batch-0001",
            mode=BatchMode.COMMIT,
        )
        with self.assertRaises(MspError) as raised:
            self.service.apply(ApplyRequest(project=session.project, batch=stale_batch))
        self.assertEqual(raised.exception.code, ErrorCode.STALE_STATE)

    def test_destructive_commit_requires_matching_plan_confirmation(self) -> None:
        session, receipt = self._seed_two_tasks()
        operations = (DeleteTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=2)),)
        unconfirmed = ApplyRequest(
            project=session.project,
            batch=OperationBatch(
                operations=operations,
                expected_state=receipt.state_after,
                idempotency_key="delete-task-0001",
                mode=BatchMode.COMMIT,
            ),
        )
        with self.assertRaises(MspError) as raised:
            self.service.apply(unconfirmed)
        self.assertEqual(raised.exception.code, ErrorCode.CONFIRMATION_REQUIRED)

        plan = self.service.apply(
            ApplyRequest(
                project=session.project,
                batch=unconfirmed.batch.model_copy(update={"mode": BatchMode.PLAN}),
            )
        )
        self.assertTrue(plan.confirmation_required)
        with self.assertRaises(MspError) as mismatch:
            self.service.apply(
                ApplyRequest(
                    project=session.project,
                    batch=unconfirmed.batch.model_copy(update={"confirmation_token": "wrong"}),
                )
            )
        self.assertEqual(mismatch.exception.code, ErrorCode.CONFIRMATION_MISMATCH)
        confirmed = self.service.apply(
            ApplyRequest(
                project=session.project,
                batch=unconfirmed.batch.model_copy(update={"confirmation_token": plan.confirmation_token}),
            )
        )
        self.assertEqual(confirmed.impact["task_count"], 1)

    def test_dependency_cycle_is_rejected(self) -> None:
        session, receipt = self._seed_two_tasks()
        cycle = OperationBatch(
            operations=(
                AddDependency(
                    predecessor=ObjectRef(kind=ObjectKind.TASK, unique_id=2),
                    successor=ObjectRef(kind=ObjectKind.TASK, unique_id=1),
                ),
            ),
            expected_state=receipt.state_after,
            idempotency_key="cycle-test-0001",
            mode=BatchMode.COMMIT,
        )
        with self.assertRaises(MspError) as raised:
            self.service.apply(ApplyRequest(project=session.project, batch=cycle))
        self.assertEqual(raised.exception.code, ErrorCode.DEPENDENCY_CYCLE)

    def test_attached_projects_are_detach_only(self) -> None:
        attached = self.service.project(ProjectRequest(action=ProjectAction.ATTACH, name="User Plan", idempotency_key="attach-user-plan-0001"))
        with self.assertRaises(MspError) as raised:
            self.service.project(
                ProjectRequest(
                    action=ProjectAction.CLOSE,
                    project=attached.project,
                    expected_state=attached.state,
                    idempotency_key="close-user-0001",
                    close_disposition=CloseDisposition.SAVE_AND_CLOSE,
                )
            )
        self.assertEqual(raised.exception.code, ErrorCode.OWNERSHIP_VIOLATION)
        result = self.service.project(
            ProjectRequest(action=ProjectAction.DETACH, project=attached.project)
        )
        self.assertTrue(result["detached"])
        with self.assertRaises(MspError) as missing:
            self.backend.get_session(attached.project)
        self.assertEqual(missing.exception.code, ErrorCode.SESSION_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
