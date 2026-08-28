from __future__ import annotations

import copy
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .backend import BackendQueryPage
from .errors import ErrorCode, MspError
from .models import (
    AddDependency,
    AnalysisKind,
    Atomicity,
    CapabilityReport,
    ChangeReceipt,
    ClearBaseline,
    CloseDisposition,
    CommitState,
    ContractFidelity,
    CreateAssignment,
    CreateCalendar,
    CreateResource,
    CreateTask,
    DeleteAssignment,
    DeleteCalendar,
    DeleteResource,
    DeleteTask,
    DesktopSmoke,
    ExportOptions,
    MoveTask,
    ObjectKind,
    ObjectRef,
    Operation,
    Ownership,
    ProjectRef,
    ProjectSession,
    ProjectState,
    QueryEntity,
    RemoveDependency,
    ResourceType,
    ScheduleCommand,
    ScheduleOptions,
    SetBaseline,
    SetStatusDate,
    StatusOperation,
    TaskProgressUpdate,
    TimephasedWorkUpdate,
    UpdateAssignment,
    UpdateCalendar,
    UpdateProjectProperties,
    UpdateResource,
    UpdateTask,
    VerificationLevel,
)


TOOL_NAMES = (
    "msp_capabilities",
    "msp_project",
    "msp_query",
    "msp_apply",
    "msp_schedule",
    "msp_status",
    "msp_analyze",
    "msp_export",
)
MOCK_COMMITTED_AT = datetime(2000, 1, 1, tzinfo=timezone.utc)


@dataclass
class _Task:
    unique_id: int
    name: str
    duration_minutes: int
    milestone: bool
    parent_id: int | None
    after_id: int | None
    fixed_cost: Decimal
    cost_accrual: str
    percent_complete: Decimal = Decimal("0")
    actual_duration_minutes: int | None = None
    remaining_duration_minutes: int | None = None
    actual_work_minutes: int | None = None
    remaining_work_minutes: int | None = None
    actual_start: datetime | None = None
    actual_finish: datetime | None = None


@dataclass
class _Resource:
    unique_id: int
    name: str
    resource_type: ResourceType
    max_units_percent: Decimal
    standard_rate: Decimal
    overtime_rate_per_hour: Decimal
    cost_per_use: Decimal
    material_label: str | None


@dataclass
class _Assignment:
    unique_id: int
    task_id: int
    resource_id: int
    units_percent: Decimal
    material_units: Decimal | None
    work_minutes: int | None
    cost_rate_table: str
    cost: Decimal | None = None
    timephased_actual_work: dict[str, int] = field(default_factory=dict)


@dataclass
class _Calendar:
    unique_id: int
    name: str
    base_calendar_id: int | None
    weekly: tuple[dict[str, Any], ...]
    exceptions: tuple[dict[str, Any], ...]


@dataclass
class _Project:
    ref: ProjectRef
    ownership: Ownership
    name: str
    path: str | None
    dirty: bool = False
    tasks: dict[int, _Task] = field(default_factory=dict)
    resources: dict[int, _Resource] = field(default_factory=dict)
    assignments: dict[int, _Assignment] = field(default_factory=dict)
    calendars: dict[int, _Calendar] = field(default_factory=dict)
    dependencies: set[tuple[int, int, str, int]] = field(default_factory=set)
    baselines: set[int] = field(default_factory=set)
    properties: dict[str, Any] = field(default_factory=dict)
    status_date: datetime | None = None
    next_ids: dict[ObjectKind, int] = field(
        default_factory=lambda: {
            ObjectKind.TASK: 1,
            ObjectKind.RESOURCE: 1,
            ObjectKind.ASSIGNMENT: 1,
            ObjectKind.CALENDAR: 1,
        }
    )


class MockProjectBackend:
    """Deterministic contract backend. It does not emulate Project scheduling."""

    def __init__(self, *, instance_namespace: str | None = None) -> None:
        self._projects: dict[str, _Project] = {}
        self._instance_namespace = instance_namespace or secrets.token_hex(8)
        self._next_session = 1
        self._next_project = 1

    def capabilities(self) -> CapabilityReport:
        operations = (
            "create_task",
            "update_task",
            "move_task",
            "delete_task",
            "create_resource",
            "update_resource",
            "delete_resource",
            "create_assignment",
            "update_assignment",
            "delete_assignment",
            "add_dependency",
            "remove_dependency",
            "create_calendar",
            "update_calendar",
            "delete_calendar",
            "update_project_properties",
            "set_baseline",
            "clear_baseline",
        )
        return CapabilityReport(
            backend="mock",
            available=True,
            installed=False,
            contract_fidelity=ContractFidelity.CONTRACT_ONLY,
            scheduling_fidelity=ContractFidelity.CONTRACT_ONLY,
            desktop_smoke=DesktopSmoke.NOT_VERIFIED,
            supported_tools=TOOL_NAMES,
            supported_operations=operations,
            safety_classes={
                "msp_capabilities": "read_only",
                "msp_project": "lifecycle_guarded",
                "msp_query": "read_only",
                "msp_apply": "write_guarded",
                "msp_schedule": "write_guarded",
                "msp_status": "write_guarded",
                "msp_analyze": "read_only",
                "msp_export": "confirmation_required",
            },
            notes=(
                "contract_only: deterministic data semantics, not Microsoft Project scheduling parity",
                "desktop smoke is not_verified",
            ),
        )

    def _new_project(self, name: str, path: str | None, ownership: Ownership) -> ProjectSession:
        session_id = f"mock-session-{self._instance_namespace}-{self._next_session:04d}"
        project_key = f"mock-project-{self._instance_namespace}-{self._next_project:04d}"
        self._next_session += 1
        self._next_project += 1
        ref = ProjectRef(session_id=session_id, project_key=project_key)
        self._projects[session_id] = _Project(ref=ref, ownership=ownership, name=name, path=path)
        return self.get_session(ref)

    def create_project(self, *, name: str, path: str | None) -> ProjectSession:
        return self._new_project(name, path, Ownership.SERVER_OWNED)

    def open_project(self, *, path: str) -> ProjectSession:
        name = path.replace("/", "\\").rsplit("\\", 1)[-1]
        return self._new_project(name, path, Ownership.SERVER_OWNED)

    def attach_project(self, *, name: str | None) -> ProjectSession:
        return self._new_project(name or "Attached Project", None, Ownership.ATTACHED_USER_OWNED)

    def _get(self, project: ProjectRef) -> _Project:
        stored = self._projects.get(project.session_id)
        if stored is None:
            raise MspError(ErrorCode.SESSION_NOT_FOUND, "Project session was not found")
        if stored.ref.project_key != project.project_key:
            raise MspError(
                ErrorCode.PROJECT_IDENTITY_CHANGED,
                "The project identity no longer matches this session",
                details={"expected": stored.ref.project_key, "received": project.project_key},
            )
        return stored

    def get_session(self, project: ProjectRef) -> ProjectSession:
        stored = self._get(project)
        return ProjectSession(
            project=stored.ref,
            ownership=stored.ownership,
            name=stored.name,
            path=stored.path,
            dirty=stored.dirty,
            state=self.current_state(project),
        )

    def _require_expected(self, project: ProjectRef, expected_state: ProjectState) -> None:
        actual = self.current_state(project)
        if actual != expected_state:
            raise MspError(
                ErrorCode.STALE_STATE,
                "Project changed before backend dispatch",
                details={"expected": expected_state.token, "actual": actual.token},
            )

    def save_project(
        self, project: ProjectRef, *, path: str | None, expected_state: ProjectState
    ) -> ProjectSession:
        self._require_expected(project, expected_state)
        stored = self._get(project)
        if path is not None:
            stored.path = path
        stored.dirty = False
        return self.get_session(project)

    def detach_project(self, project: ProjectRef) -> None:
        stored = self._get(project)
        if stored.ownership != Ownership.ATTACHED_USER_OWNED:
            raise MspError(ErrorCode.OWNERSHIP_VIOLATION, "Server-owned projects must be closed, not detached")
        del self._projects[project.session_id]

    def close_project(
        self, project: ProjectRef, disposition: CloseDisposition, *, expected_state: ProjectState
    ) -> None:
        self._require_expected(project, expected_state)
        stored = self._get(project)
        if stored.ownership == Ownership.ATTACHED_USER_OWNED:
            raise MspError(ErrorCode.OWNERSHIP_VIOLATION, "Attached user-owned projects are detach-only")
        if stored.dirty and disposition == CloseDisposition.REFUSE_IF_DIRTY:
            raise MspError(ErrorCode.INVALID_REQUEST, "Dirty project requires an explicit close disposition")
        del self._projects[project.session_id]

    @staticmethod
    def _json(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: MockProjectBackend._json(item) for key, item in value.items()}
        if isinstance(value, (tuple, list, set)):
            return [MockProjectBackend._json(item) for item in value]
        return value

    def _canonical(self, stored: _Project) -> dict[str, Any]:
        return self._json(
            {
                "name": stored.name,
                "path": stored.path,
                "properties": stored.properties,
                "status_date": stored.status_date,
                "tasks": [vars(item) for item in sorted(stored.tasks.values(), key=lambda value: value.unique_id)],
                "resources": [
                    vars(item) for item in sorted(stored.resources.values(), key=lambda value: value.unique_id)
                ],
                "assignments": [
                    vars(item) for item in sorted(stored.assignments.values(), key=lambda value: value.unique_id)
                ],
                "calendars": [
                    vars(item) for item in sorted(stored.calendars.values(), key=lambda value: value.unique_id)
                ],
                "dependencies": sorted(stored.dependencies),
                "baselines": sorted(stored.baselines),
            }
        )

    def current_state(self, project: ProjectRef) -> ProjectState:
        payload = json.dumps(self._canonical(self._get(project)), sort_keys=True, separators=(",", ":"))
        return ProjectState(token=f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}")

    def _task_item(self, item: _Task) -> dict[str, Any]:
        data = self._json(vars(item))
        data["ref"] = ObjectRef(kind=ObjectKind.TASK, unique_id=item.unique_id).model_dump(mode="json")
        return data

    def _resource_item(self, item: _Resource) -> dict[str, Any]:
        data = self._json(vars(item))
        data["resource_type"] = item.resource_type.value
        data["standard_rate_basis"] = (
            "hour" if item.resource_type == ResourceType.WORK
            else "material_unit" if item.resource_type == ResourceType.MATERIAL
            else None
        )
        if item.resource_type != ResourceType.WORK:
            data["max_units_percent"] = None
            data["overtime_rate_per_hour"] = None
        if item.resource_type == ResourceType.COST:
            data["standard_rate"] = None
            data["cost_per_use"] = None
        if item.resource_type != ResourceType.MATERIAL:
            data["material_label"] = None
        data["ref"] = ObjectRef(kind=ObjectKind.RESOURCE, unique_id=item.unique_id).model_dump(mode="json")
        return data

    def _assignment_item(self, item: _Assignment, stored: _Project) -> dict[str, Any]:
        data = self._json(vars(item))
        resource_type = stored.resources[item.resource_id].resource_type
        if resource_type != ResourceType.WORK:
            data["units_percent"] = None
            data["work_minutes"] = None
        if resource_type != ResourceType.MATERIAL:
            data["material_units"] = None
        if resource_type != ResourceType.COST:
            data["cost"] = None
        else:
            data["cost_rate_table"] = None
        data["ref"] = ObjectRef(kind=ObjectKind.ASSIGNMENT, unique_id=item.unique_id).model_dump(mode="json")
        return data

    def _calendar_item(self, item: _Calendar) -> dict[str, Any]:
        data = self._json(vars(item))
        data["ref"] = ObjectRef(kind=ObjectKind.CALENDAR, unique_id=item.unique_id).model_dump(mode="json")
        return data

    def query(
        self,
        project: ProjectRef,
        entity: QueryEntity,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int,
    ) -> BackendQueryPage:
        stored = self._get(project)
        if entity == QueryEntity.PROJECT:
            items = [{"name": stored.name, "path": stored.path, "ownership": stored.ownership.value, **stored.properties}]
        elif entity == QueryEntity.TASK:
            items = [self._task_item(item) for item in sorted(stored.tasks.values(), key=lambda value: value.unique_id)]
        elif entity == QueryEntity.RESOURCE:
            items = [
                self._resource_item(item) for item in sorted(stored.resources.values(), key=lambda value: value.unique_id)
            ]
        elif entity == QueryEntity.ASSIGNMENT:
            items = [
                self._assignment_item(item, stored)
                for item in sorted(stored.assignments.values(), key=lambda value: value.unique_id)
            ]
        elif entity == QueryEntity.CALENDAR:
            items = [
                self._calendar_item(item) for item in sorted(stored.calendars.values(), key=lambda value: value.unique_id)
            ]
        elif entity == QueryEntity.DEPENDENCY:
            items = [
                {
                    "predecessor": ObjectRef(kind=ObjectKind.TASK, unique_id=pred).model_dump(mode="json"),
                    "successor": ObjectRef(kind=ObjectKind.TASK, unique_id=succ).model_dump(mode="json"),
                    "dependency_type": dep_type,
                    "lag_minutes": lag,
                }
                for pred, succ, dep_type, lag in sorted(stored.dependencies)
            ]
        elif entity == QueryEntity.BASELINE:
            items = [{"baseline": baseline, "set": True} for baseline in sorted(stored.baselines)]
        elif entity == QueryEntity.STATUS:
            items = [{"status_date": self._json(stored.status_date)}]
        else:
            items = []
        allowed = set(items[0]) if items else set()
        unknown = set(fields) - allowed
        if unknown:
            raise MspError(
                ErrorCode.INVALID_REQUEST,
                "Query contains unsupported projection fields",
                details={"unsupported_fields": sorted(unknown)},
            )
        if fields:
            items = [{key: item[key] for key in fields} for item in items]
        page = items[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(items) else None
        return BackendQueryPage(items=tuple(page), next_offset=next_offset, state=self.current_state(project))

    def dependency_edges(self, project: ProjectRef) -> tuple[tuple[int, int], ...]:
        return tuple(sorted((pred, succ) for pred, succ, _, _ in self._get(project).dependencies))

    def task_parent_edges(self, project: ProjectRef) -> tuple[tuple[int, int], ...]:
        return tuple(
            sorted(
                (task.unique_id, task.parent_id)
                for task in self._get(project).tasks.values()
                if task.parent_id is not None
            )
        )

    def resolve_ref(self, project: ProjectRef, ref: ObjectRef) -> int:
        stored = self._get(project)
        collection: dict[int, Any]
        if ref.kind == ObjectKind.TASK:
            collection = stored.tasks
        elif ref.kind == ObjectKind.RESOURCE:
            collection = stored.resources
        elif ref.kind == ObjectKind.ASSIGNMENT:
            collection = stored.assignments
        elif ref.kind == ObjectKind.CALENDAR:
            collection = stored.calendars
        else:
            raise MspError(ErrorCode.INVALID_REQUEST, "Unsupported object reference kind")
        if ref.unique_id is None or ref.unique_id not in collection:
            raise MspError(
                ErrorCode.INVALID_REQUEST,
                "Object reference could not be resolved",
                details={"ref": ref.model_dump(mode="json")},
            )
        return ref.unique_id

    def _next_id(self, stored: _Project, kind: ObjectKind) -> int:
        value = stored.next_ids[kind]
        stored.next_ids[kind] += 1
        return value

    def apply_operations(
        self,
        project: ProjectRef,
        operations: tuple[Operation, ...],
        *,
        idempotency_key: str,
        verification: VerificationLevel,
        expected_state: ProjectState,
    ) -> ChangeReceipt:
        self._require_expected(project, expected_state)
        stored = self._get(project)
        snapshot = copy.deepcopy(stored)
        state_before = self.current_state(project)
        local: dict[tuple[ObjectKind, str], int] = {}
        observed: list[dict[str, Any]] = []

        def resolve(ref: ObjectRef) -> int:
            if ref.client_ref is not None:
                try:
                    return local[(ref.kind, ref.client_ref)]
                except KeyError as exc:
                    raise MspError(ErrorCode.INVALID_REQUEST, "Unknown batch-local object reference") from exc
            return self.resolve_ref(project, ref)

        try:
            for operation in operations:
                observation: dict[str, Any] = {"op": operation.op, "applied": True}
                if isinstance(operation, CreateTask):
                    uid = self._next_id(stored, ObjectKind.TASK)
                    stored.tasks[uid] = _Task(
                        uid,
                        operation.name,
                        operation.duration_minutes,
                        operation.milestone,
                        resolve(operation.parent) if operation.parent else None,
                        resolve(operation.after) if operation.after else None,
                        operation.fixed_cost,
                        operation.cost_accrual.value,
                    )
                    local[(ObjectKind.TASK, operation.client_ref)] = uid
                    observation.update(
                        {
                            "client_ref": operation.client_ref,
                            "ref": ObjectRef(kind=ObjectKind.TASK, unique_id=uid).model_dump(mode="json"),
                        }
                    )
                elif isinstance(operation, UpdateTask):
                    uid = resolve(operation.task)
                    task = stored.tasks[uid]
                    for name in ("name", "duration_minutes", "fixed_cost"):
                        value = getattr(operation, name)
                        if value is not None:
                            setattr(task, name, value)
                    if operation.cost_accrual is not None:
                        task.cost_accrual = operation.cost_accrual.value
                    if operation.duration_minutes is not None:
                        task.milestone = operation.duration_minutes == 0
                elif isinstance(operation, MoveTask):
                    uid = resolve(operation.task)
                    task = stored.tasks[uid]
                    task.parent_id = None if operation.to_root else (resolve(operation.parent) if operation.parent else task.parent_id)
                    task.after_id = resolve(operation.after) if operation.after else None
                elif isinstance(operation, DeleteTask):
                    uid = resolve(operation.task)
                    children = {task.unique_id for task in stored.tasks.values() if task.parent_id == uid}
                    if children and not operation.recursive:
                        raise MspError(ErrorCode.INVALID_REQUEST, "Task has children; recursive=true is required")
                    deleting = {uid}
                    while operation.recursive:
                        added = {task.unique_id for task in stored.tasks.values() if task.parent_id in deleting}
                        if added <= deleting:
                            break
                        deleting |= added
                    for task_id in deleting:
                        stored.tasks.pop(task_id, None)
                    stored.dependencies = {edge for edge in stored.dependencies if edge[0] not in deleting and edge[1] not in deleting}
                    stored.assignments = {
                        key: assignment
                        for key, assignment in stored.assignments.items()
                        if assignment.task_id not in deleting
                    }
                elif isinstance(operation, CreateResource):
                    uid = self._next_id(stored, ObjectKind.RESOURCE)
                    stored.resources[uid] = _Resource(
                        uid,
                        operation.name,
                        operation.resource_type,
                        operation.max_units_percent,
                        operation.standard_rate,
                        operation.overtime_rate_per_hour,
                        operation.cost_per_use,
                        operation.material_label,
                    )
                    local[(ObjectKind.RESOURCE, operation.client_ref)] = uid
                    observation.update(
                        {
                            "client_ref": operation.client_ref,
                            "ref": ObjectRef(kind=ObjectKind.RESOURCE, unique_id=uid).model_dump(mode="json"),
                        }
                    )
                elif isinstance(operation, UpdateResource):
                    uid = resolve(operation.resource)
                    resource = stored.resources[uid]
                    for name in (
                        "name",
                        "max_units_percent",
                        "standard_rate",
                        "overtime_rate_per_hour",
                        "cost_per_use",
                        "material_label",
                    ):
                        value = getattr(operation, name)
                        if value is not None:
                            setattr(resource, name, value)
                elif isinstance(operation, DeleteResource):
                    uid = resolve(operation.resource)
                    if any(assignment.resource_id == uid for assignment in stored.assignments.values()):
                        raise MspError(ErrorCode.INVALID_REQUEST, "Assigned resources cannot be deleted")
                    del stored.resources[uid]
                elif isinstance(operation, CreateAssignment):
                    uid = self._next_id(stored, ObjectKind.ASSIGNMENT)
                    stored.assignments[uid] = _Assignment(
                        uid,
                        resolve(operation.task),
                        resolve(operation.resource),
                        operation.units_percent,
                        operation.material_units,
                        operation.work_minutes,
                        operation.cost_rate_table,
                        operation.cost,
                    )
                    local[(ObjectKind.ASSIGNMENT, operation.client_ref)] = uid
                    observation.update(
                        {
                            "client_ref": operation.client_ref,
                            "ref": ObjectRef(kind=ObjectKind.ASSIGNMENT, unique_id=uid).model_dump(mode="json"),
                        }
                    )
                elif isinstance(operation, UpdateAssignment):
                    uid = resolve(operation.assignment)
                    assignment = stored.assignments[uid]
                    for name in ("units_percent", "material_units", "work_minutes", "cost_rate_table", "cost"):
                        value = getattr(operation, name)
                        if value is not None:
                            setattr(assignment, name, value)
                elif isinstance(operation, DeleteAssignment):
                    del stored.assignments[resolve(operation.assignment)]
                elif isinstance(operation, AddDependency):
                    stored.dependencies.add(
                        (
                            resolve(operation.predecessor),
                            resolve(operation.successor),
                            operation.dependency_type,
                            operation.lag_minutes,
                        )
                    )
                elif isinstance(operation, RemoveDependency):
                    pred, succ = resolve(operation.predecessor), resolve(operation.successor)
                    stored.dependencies = {edge for edge in stored.dependencies if edge[0] != pred or edge[1] != succ}
                elif isinstance(operation, CreateCalendar):
                    uid = self._next_id(stored, ObjectKind.CALENDAR)
                    stored.calendars[uid] = _Calendar(
                        uid,
                        operation.name,
                        resolve(operation.base_calendar) if operation.base_calendar else None,
                        tuple(day.model_dump(mode="json") for day in operation.weekly),
                        tuple(item.model_dump(mode="json") for item in operation.exceptions),
                    )
                    local[(ObjectKind.CALENDAR, operation.client_ref)] = uid
                    observation.update(
                        {
                            "client_ref": operation.client_ref,
                            "ref": ObjectRef(kind=ObjectKind.CALENDAR, unique_id=uid).model_dump(mode="json"),
                        }
                    )
                elif isinstance(operation, UpdateCalendar):
                    uid = resolve(operation.calendar)
                    calendar = stored.calendars[uid]
                    if operation.name is not None:
                        calendar.name = operation.name
                    if operation.weekly is not None:
                        calendar.weekly = tuple(day.model_dump(mode="json") for day in operation.weekly)
                    if operation.exceptions is not None:
                        calendar.exceptions = tuple(item.model_dump(mode="json") for item in operation.exceptions)
                elif isinstance(operation, DeleteCalendar):
                    uid = resolve(operation.calendar)
                    if any(calendar.base_calendar_id == uid for calendar in stored.calendars.values()):
                        raise MspError(ErrorCode.INVALID_REQUEST, "A base calendar in use cannot be deleted")
                    del stored.calendars[uid]
                elif isinstance(operation, UpdateProjectProperties):
                    stored.properties.update(operation.model_dump(mode="json", exclude={"op"}, exclude_none=True))
                elif isinstance(operation, SetBaseline):
                    stored.baselines.add(operation.baseline)
                elif isinstance(operation, ClearBaseline):
                    stored.baselines.discard(operation.baseline)
                observed.append(observation)
            stored.dirty = True
        except Exception:
            self._projects[project.session_id] = snapshot
            raise

        state_after = self.current_state(project)
        requested = tuple(operation.model_dump(mode="json") for operation in operations)
        digest = hashlib.sha256(
            json.dumps([idempotency_key, state_before.token, state_after.token]).encode("utf-8")
        ).hexdigest()[:24]
        return ChangeReceipt(
            receipt_id=f"mock-receipt-{digest}",
            project=project,
            idempotency_key=idempotency_key,
            state_before=state_before,
            state_after=state_after,
            requested=requested,
            observed=tuple(observed),
            verification=verification,
            atomicity=Atomicity.UNDO_ATOMIC,
            undo_available=True,
            commit_state=CommitState.COMMITTED,
            impact={
                "task_count": len(stored.tasks),
                "resource_count": len(stored.resources),
                "assignment_count": len(stored.assignments),
                "calendar_count": len(stored.calendars),
                "dependency_count": len(stored.dependencies),
            },
            committed_at=MOCK_COMMITTED_AT,
        )

    def schedule(
        self,
        project: ProjectRef,
        command: ScheduleCommand,
        options: ScheduleOptions,
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]:
        self._require_expected(project, expected_state)
        self._get(project)
        return {
            "command": command.value,
            "executed": True,
            "scheduling_fidelity": ContractFidelity.CONTRACT_ONLY.value,
            "state": self.current_state(project).model_dump(mode="json"),
        }

    def update_status(
        self,
        project: ProjectRef,
        updates: tuple[StatusOperation, ...],
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]:
        self._require_expected(project, expected_state)
        stored = self._get(project)
        snapshot = copy.deepcopy(stored)
        try:
            for update in updates:
                if isinstance(update, SetStatusDate):
                    stored.status_date = update.status_date
                elif isinstance(update, TaskProgressUpdate):
                    task = stored.tasks[self.resolve_ref(project, update.task)]
                    for name in (
                        "percent_complete",
                        "actual_duration_minutes",
                        "remaining_duration_minutes",
                        "actual_work_minutes",
                        "remaining_work_minutes",
                        "actual_start",
                        "actual_finish",
                    ):
                        value = getattr(update, name)
                        if value is not None:
                            setattr(task, name, value)
                elif isinstance(update, TimephasedWorkUpdate):
                    assignment = stored.assignments[self.resolve_ref(project, update.assignment)]
                    assignment.timephased_actual_work[update.date.isoformat()] = update.actual_work_minutes
            stored.dirty = True
        except Exception:
            self._projects[project.session_id] = snapshot
            raise
        return {
            "updated": len(updates),
            "scheduling_fidelity": ContractFidelity.CONTRACT_ONLY.value,
            "state": self.current_state(project).model_dump(mode="json"),
        }

    def analyze(self, project: ProjectRef, analysis: AnalysisKind, baseline: int | None) -> dict[str, Any]:
        stored = self._get(project)
        return {
            "analysis": analysis.value,
            "baseline": baseline,
            "task_count": len(stored.tasks),
            "dependency_count": len(stored.dependencies),
            "scheduling_fidelity": ContractFidelity.CONTRACT_ONLY.value,
        }

    def export(
        self,
        project: ProjectRef,
        format: str,
        destination: str,
        options: ExportOptions,
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]:
        self._require_expected(project, expected_state)
        self._get(project)
        return {
            "format": format,
            "destination": destination,
            "written": False,
            "reason": "contract_only mock backend does not write exports",
        }

    def ownership(self, project: ProjectRef) -> Ownership:
        return self._get(project).ownership

    def plan_atomicity(self, request_family: str, operations: tuple[Operation, ...] = ()) -> Atomicity:
        if request_family in {"apply", "status"}:
            return Atomicity.UNDO_ATOMIC
        if request_family in {"save", "close", "schedule"}:
            return Atomicity.CHECKPOINTED
        return Atomicity.NON_ATOMIC

    def shutdown(self) -> None:
        return None
