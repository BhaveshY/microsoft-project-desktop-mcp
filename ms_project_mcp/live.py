from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import secrets
import shutil
import threading
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol

from .backend import BackendQueryPage
from .detection import probe_desktop_project
from .errors import BackendExecutionError, DispatchState, ErrorCode, MspError
from .mock import TOOL_NAMES
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
    DesktopProjectDetection,
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
    ScheduleFrom,
    ScheduleCommand,
    ScheduleOptions,
    SetBaseline,
    SetStatusDate,
    StatusOperation,
    TaskProgressUpdate,
    TaskConstraintType,
    TaskType,
    TimephasedWorkUpdate,
    UpdateAssignment,
    UpdateCalendar,
    UpdateProjectProperties,
    UpdateResource,
    UpdateTask,
    VerificationLevel,
    Weekday,
)
from .sta import StaCallTimeout, StaHost, StaHostClosedError, StaHostState, StaWorkerFailedError


class AutomationFactory(Protocol):
    def create_application(self) -> Any: ...

    def get_active_application(self) -> Any: ...


class _PyWin32AutomationFactory:
    def __init__(self) -> None:
        self._client = importlib.import_module("win32com.client")

    def create_application(self) -> Any:
        return self._client.DispatchEx("MSProject.Application")

    def get_active_application(self) -> Any:
        return self._client.GetActiveObject("MSProject.Application")


def load_automation_factory() -> AutomationFactory:
    """Load the automation client lazily on the STA worker."""
    return _PyWin32AutomationFactory()


@dataclass
class _LiveSession:
    ref: ProjectRef
    ownership: Ownership
    app: Any
    project: Any
    normalized_full_name: str
    native_guid: str | None


@dataclass(frozen=True)
class _CreatedTaskPlacement:
    task: Any
    before_unique_id: int | None
    subtree_tail_unique_id: int | None
    parent_unique_id: int | None
    after_unique_id: int | None


class _RereadMismatch(RuntimeError):
    pass


_TASK_LINK_FROM_NATIVE = {0: "FF", 1: "FS", 2: "SF", 3: "SS"}
_TASK_LINK_TO_NATIVE = {value: key for key, value in _TASK_LINK_FROM_NATIVE.items()}
_RESOURCE_TYPE_FROM_NATIVE = {0: "work", 1: "material", 2: "cost"}
_RESOURCE_TYPE_TO_NATIVE = {value: key for key, value in _RESOURCE_TYPE_FROM_NATIVE.items()}
_COST_ACCRUAL_FROM_NATIVE = {1: "start", 2: "end", 3: "prorated"}
_COST_ACCRUAL_TO_NATIVE = {value: key for key, value in _COST_ACCRUAL_FROM_NATIVE.items()}
_COST_RATE_FROM_NATIVE = {0: "A", 1: "B", 2: "C", 3: "D", 4: "E"}
_COST_RATE_TO_NATIVE = {value: key for key, value in _COST_RATE_FROM_NATIVE.items()}
_TASK_TYPE_FROM_NATIVE = {0: "fixed_units", 1: "fixed_duration", 2: "fixed_work"}
_TASK_TYPE_TO_NATIVE = {value: key for key, value in _TASK_TYPE_FROM_NATIVE.items()}
_TASK_CONSTRAINT_FROM_NATIVE = {
    0: "as_soon_as_possible",
    1: "as_late_as_possible",
    2: "must_start_on",
    3: "must_finish_on",
    4: "start_no_earlier_than",
    5: "start_no_later_than",
    6: "finish_no_earlier_than",
    7: "finish_no_later_than",
}
_TASK_CONSTRAINT_TO_NATIVE = {value: key for key, value in _TASK_CONSTRAINT_FROM_NATIVE.items()}
_SCHEDULE_FROM_NATIVE = {1: "start", 2: "finish"}
_SCHEDULE_TO_NATIVE = {value: key for key, value in _SCHEDULE_FROM_NATIVE.items()}
_BASELINE_INTO = {0: 0, **{index: index + 10 for index in range(1, 11)}}
_WEEKDAY_TO_NATIVE = {
    Weekday.SUNDAY: 1,
    Weekday.MONDAY: 2,
    Weekday.TUESDAY: 3,
    Weekday.WEDNESDAY: 4,
    Weekday.THURSDAY: 5,
    Weekday.FRIDAY: 6,
    Weekday.SATURDAY: 7,
}
_WEEKDAY_FROM_NATIVE = {value: key.value for key, value in _WEEKDAY_TO_NATIVE.items()}
# Project PjAssignmentTimescaledData.pjAssignmentTimescaledActualWork and
# PjTimescaleUnit.pjTimescaleDays, used by Assignment.TimeScaleData.
_PJ_ASSIGNMENT_TIMESCALED_ACTUAL_WORK = 10
_PJ_TIMESCALE_DAYS = 4


class LiveProjectBackend:
    def __init__(
        self,
        *,
        detection: DesktopProjectDetection | None = None,
        sta_host: StaHost | None = None,
        automation_factory_provider: Callable[[], AutomationFactory] = load_automation_factory,
        call_timeout: float | None = 120.0,
        server_app_visible: bool = True,
        instance_namespace: str | None = None,
    ) -> None:
        self.detection = detection or probe_desktop_project()
        self._sta = sta_host or StaHost()
        self._automation_factory_provider = automation_factory_provider
        self._call_timeout = call_timeout
        self._server_app_visible = server_app_visible
        self._instance_namespace = instance_namespace or secrets.token_hex(8)
        self._start_lock = threading.Lock()
        self._factory: AutomationFactory | None = None
        self._server_app: Any = None
        self._sessions: dict[str, _LiveSession] = {}
        self._next_session = 1

    def capabilities(self) -> CapabilityReport:
        ready = (
            self.detection.windows
            and self.detection.com_registered
            and self.detection.pywin32_importable
        )
        return CapabilityReport(
            backend="live",
            available=ready,
            installed=self.detection.com_registered,
            contract_fidelity=ContractFidelity.LIVE_NATIVE,
            scheduling_fidelity=ContractFidelity.LIVE_NATIVE,
            desktop_smoke=DesktopSmoke.NOT_VERIFIED,
            activates_desktop=False,
            supported_tools=TOOL_NAMES,
            supported_operations=(
                "create_task:parent_after", "create_task:advanced_planning", "update_task",
                "update_task:advanced_planning",
                "delete_task:non_recursive", "delete_task:recursive",
                "create_resource:details_calendar",
                "update_resource:details_calendar", "delete_resource",
                "create_assignment", "update_assignment", "delete_assignment",
                "add_dependency", "remove_dependency", "update_project_properties:scheduling",
                "create_calendar", "update_calendar", "delete_calendar",
                "set_baseline", "clear_baseline", "schedule:calculate", "schedule:level",
                "schedule:clear_leveling", "schedule:reschedule", "status:task_progress",
                "status:set_status_date", "status:timephased_actual_work_daily",
                "analyze", "export:pdf", "export:mpp",
                "project:create_from_template",
            ),
            safety_classes={
                "msp_capabilities": "read_only",
                "msp_project": "lifecycle_guarded",
                "msp_query": "read_only",
                "msp_apply": "native_reread_guarded",
                "msp_schedule": "native_project_authority",
                "msp_status": "native_reread_guarded",
                "msp_analyze": "read_only",
                "msp_export": "destination_guarded",
            },
            notes=(
                "Live writes use state checks inside the owning STA immediately before mutation",
                "Task creation supports advanced scheduling and stable parent/after placement; physical row moves remain unsupported",
                "Daily assignment actual work uses native timephased buckets with exact reread verification",
                "Capabilities probing does not activate Microsoft Project",
            ),
            detection=self.detection,
        )

    def create_project(
        self, *, name: str, path: str | None, template_path: str | None = None
    ) -> ProjectSession:
        validated_path = self._validated_save_target(path, current="", require_new=True) if path else None
        validated_template = self._validated_template_target(template_path) if template_path else None

        def create() -> ProjectSession:
            app = self._server_application()
            project = self._invoke(
                (lambda: app.Projects.Add(False, validated_template, False))
                if validated_template
                else app.Projects.Add
            )
            if name:
                # Project.Name is read-only. Preserve the requested logical label
                # in documented summary metadata; SaveAs supplies the physical
                # file name when a path was requested.
                project.Activate()
                self._invoke(lambda: app.ProjectSummaryInfo(self._text(project.Name), name))
            session = self._bind(app, project, Ownership.SERVER_OWNED)
            if validated_path:
                try:
                    self._save_on_sta(session, validated_path)
                except Exception as exc:
                    try:
                        project.Activate()
                        cleanup_closed = self._invoke(lambda: app.FileCloseEx(0))
                        if cleanup_closed is False:
                            raise RuntimeError("Microsoft Project refused cleanup close")
                        self._sessions.pop(session.ref.session_id, None)
                    except Exception as cleanup_exc:
                        raise BackendExecutionError(
                            "Project creation failed and the new document could not be closed safely",
                            dispatch_state=DispatchState.MAY_HAVE_DISPATCHED,
                            details={
                                "cause": type(exc).__name__,
                                "cleanup_cause": type(cleanup_exc).__name__,
                            },
                        ) from exc
                    raise
            return self._public_session(session)

        return self._activation_call(create)

    def open_project(self, *, path: str) -> ProjectSession:
        validated_path = self._validated_open_target(path)

        def open_file() -> ProjectSession:
            app = self._server_application()
            opened = self._invoke(lambda: app.FileOpen(validated_path))
            if opened is False:
                raise MspError(ErrorCode.INVALID_REQUEST, "Microsoft Project refused to open the file")
            project = app.ActiveProject
            return self._public_session(self._bind(app, project, Ownership.SERVER_OWNED))

        return self._activation_call(open_file)

    def attach_project(self, *, name: str | None) -> ProjectSession:
        def attach() -> ProjectSession:
            app = self._automation_factory().get_active_application()
            project = app.ActiveProject
            if project is None:
                raise MspError(ErrorCode.PROJECT_CLOSED, "No active Microsoft Project document is available")
            if name and self._text(self._read(project, "Name", "")) != name:
                raise MspError(ErrorCode.INVALID_REQUEST, "The active project name does not match the request")
            return self._public_session(self._bind(app, project, Ownership.ATTACHED_USER_OWNED))

        return self._activation_call(attach)

    def get_session(self, project: ProjectRef) -> ProjectSession:
        return self._existing_call(lambda: self._public_session(self._session(project)))

    def save_project(
        self, project: ProjectRef, *, path: str | None, expected_state: ProjectState
    ) -> ProjectSession:
        def save() -> ProjectSession:
            session = self._session(project)
            self._require_expected_on_sta(session, expected_state)
            self._save_on_sta(session, path)
            return self._public_session(session)

        return self._existing_call(save)

    def detach_project(self, project: ProjectRef) -> None:
        def detach() -> None:
            session = self._session(project)
            if session.ownership != Ownership.ATTACHED_USER_OWNED:
                raise MspError(ErrorCode.OWNERSHIP_VIOLATION, "Server-owned projects must be closed")
            del self._sessions[project.session_id]

        self._existing_call(detach)

    def close_project(
        self, project: ProjectRef, disposition: CloseDisposition, *, expected_state: ProjectState
    ) -> None:
        def close() -> None:
            session = self._session(project)
            self._require_expected_on_sta(session, expected_state)
            if session.ownership != Ownership.SERVER_OWNED:
                raise MspError(ErrorCode.OWNERSHIP_VIOLATION, "Attached projects are detach-only")
            dirty = self._dirty(session.project)
            if dirty and disposition == CloseDisposition.REFUSE_IF_DIRTY:
                raise MspError(ErrorCode.INVALID_REQUEST, "Dirty project requires an explicit close disposition")
            if (
                disposition == CloseDisposition.SAVE_AND_CLOSE
                and not session.normalized_full_name
                and not self._text(self._read(session.project, "FullName", "")).strip()
            ):
                raise MspError(
                    ErrorCode.INVALID_REQUEST,
                    "Untitled projects require an explicit save path before save_and_close",
                )
            save_type = 1 if disposition == CloseDisposition.SAVE_AND_CLOSE else 0
            session.project.Activate()
            closed = self._invoke(lambda: session.app.FileCloseEx(save_type))
            if closed is False:
                raise MspError(ErrorCode.INVALID_REQUEST, "Microsoft Project refused or cancelled the close")
            del self._sessions[project.session_id]

        self._existing_call(close)

    def current_state(self, project: ProjectRef) -> ProjectState:
        return self._existing_call(lambda: self._state_on_sta(self._session(project)))

    def query(
        self,
        project: ProjectRef,
        entity: QueryEntity,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int,
    ) -> BackendQueryPage:
        def read_page() -> BackendQueryPage:
            session = self._session(project)
            items = self._projection(session.project, entity)
            allowed = set(items[0]) if items else self._allowed_fields(entity)
            unknown = set(fields) - allowed
            if unknown:
                raise MspError(
                    ErrorCode.INVALID_REQUEST,
                    "Query contains unsupported projection fields",
                    details={"unsupported_fields": sorted(unknown)},
                )
            if fields:
                items = [{field: item[field] for field in fields} for item in items]
            page = items[offset : offset + limit]
            next_offset = offset + limit if offset + limit < len(items) else None
            return BackendQueryPage(tuple(page), next_offset, self._state_on_sta(session))

        return self._existing_call(read_page)

    def resolve_ref(self, project: ProjectRef, ref: ObjectRef) -> int | str:
        def resolve() -> int | str:
            session = self._session(project)
            items = self._projection(session.project, self._entity_for_kind(ref.kind))
            for item in items:
                candidate = item.get("ref", {})
                if ref.unique_id is not None and candidate.get("unique_id") == ref.unique_id:
                    return ref.unique_id
                if ref.guid is not None and candidate.get("guid") == ref.guid:
                    return ref.guid
            raise MspError(ErrorCode.INVALID_REQUEST, "Object reference could not be resolved")

        return self._existing_call(resolve)

    def dependency_edges(self, project: ProjectRef) -> tuple[tuple[int, int], ...]:
        def edges() -> tuple[tuple[int, int], ...]:
            session = self._session(project)
            return tuple(
                (item["predecessor"]["unique_id"], item["successor"]["unique_id"])
                for item in self._dependencies(session.project)
            )

        return self._existing_call(edges)

    def task_parent_edges(self, project: ProjectRef) -> tuple[tuple[int, int], ...]:
        def edges() -> tuple[tuple[int, int], ...]:
            session = self._session(project)
            return tuple(
                (item["ref"]["unique_id"], item["parent_ref"]["unique_id"])
                for item in self._tasks(session.project)
                if item["parent_ref"] is not None
            )

        return self._existing_call(edges)

    def ownership(self, project: ProjectRef) -> Ownership:
        return self._existing_call(lambda: self._session(project).ownership)

    def apply_operations(
        self,
        project: ProjectRef,
        operations: tuple[Operation, ...],
        *,
        idempotency_key: str,
        verification: VerificationLevel,
        expected_state: ProjectState,
    ) -> ChangeReceipt:
        def apply() -> ChangeReceipt:
            session = self._session(project)
            self._require_expected_on_sta(session, expected_state)
            self._validate_operations_on_sta(session, operations)
            state_before = self._state_on_sta(session)
            if state_before != expected_state:
                raise MspError(ErrorCode.STALE_STATE, "Microsoft Project changed during write validation")
            app = session.app
            self._activate_session(session)
            self._invoke(lambda: app.OpenUndoTransaction(f"Microsoft Project MCP: {idempotency_key}"))
            undo_open = True
            mutation_possible = False
            local: dict[tuple[ObjectKind, str], Any] = {}
            touched: list[tuple[Operation, Any]] = []
            active_operation: Operation | None = None
            try:
                for operation in operations:
                    active_operation = operation
                    mutation_possible = True
                    native = self._apply_one_on_sta(session, operation, local)
                    touched.append((operation, native))
                self._activate_session(session)
                self._require_com_success(
                    self._invoke(app.CalculateProject),
                    "CalculateProject",
                    mismatch=True,
                )
                self._activate_session(session)
                self._invoke(app.CloseUndoTransaction)
                undo_open = False
                observed_items = []
                for operation, native in touched:
                    active_operation = operation
                    observed_items.append(
                        self._verify_operation_on_sta(session, operation, native, local)
                    )
                observed = tuple(observed_items)
                state_after = self._state_on_sta(session)
            except Exception as exc:
                if undo_open:
                    try:
                        self._activate_session(session)
                        self._invoke(app.CloseUndoTransaction)
                    except Exception:
                        pass
                if not mutation_possible:
                    raise
                try:
                    self._activate_session(session)
                    self._invoke(app.Undo)
                    restored = self._state_on_sta(session) == state_before
                except Exception as rollback_exc:
                    raise BackendExecutionError(
                        "Microsoft Project write failed and rollback could not be verified",
                        dispatch_state=DispatchState.MAY_HAVE_DISPATCHED,
                        details={
                            "cause": type(exc).__name__,
                            "cause_message": str(exc),
                            "operation": active_operation.op if active_operation else None,
                            "operation_ref": getattr(active_operation, "client_ref", None),
                            "rollback_cause": type(rollback_exc).__name__,
                        },
                    ) from exc
                if restored:
                    code = ErrorCode.VERIFICATION_FAILED if isinstance(exc, _RereadMismatch) else ErrorCode.WRITE_ROLLED_BACK
                    raise MspError(
                        code,
                        "Microsoft Project rejected or failed verification of the batch; the undo restored the original state",
                        retryable=True,
                        details={
                            "cause": type(exc).__name__,
                            "cause_message": str(exc),
                            "operation": active_operation.op if active_operation else None,
                            "operation_ref": getattr(active_operation, "client_ref", None),
                        },
                    ) from exc
                raise BackendExecutionError(
                    "Microsoft Project write failed and rollback state is uncertain",
                    dispatch_state=DispatchState.MAY_HAVE_DISPATCHED,
                    details={
                        "cause": type(exc).__name__,
                        "cause_message": str(exc),
                        "operation": active_operation.op if active_operation else None,
                        "operation_ref": getattr(active_operation, "client_ref", None),
                    },
                ) from exc
            return ChangeReceipt(
                receipt_id=f"live:{hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]}",
                project=project,
                idempotency_key=idempotency_key,
                state_before=state_before,
                state_after=state_after,
                requested=tuple(operation.model_dump(mode="json") for operation in operations),
                observed=observed,
                warnings=(),
                impact={"operation_count": len(operations)},
                verification=verification,
                atomicity=Atomicity.UNDO_ATOMIC,
                undo_available=True,
                commit_state=CommitState.COMMITTED,
            )

        return self._existing_call(apply)

    def schedule(
        self,
        project: ProjectRef,
        command: ScheduleCommand,
        options: ScheduleOptions,
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]:
        def execute() -> dict[str, Any]:
            session = self._session(project)
            self._require_expected_on_sta(session, expected_state)
            app = session.app
            before = self._state_on_sta(session)
            if before != expected_state:
                raise MspError(ErrorCode.STALE_STATE, "Microsoft Project changed during schedule validation")
            if options.clear_existing_leveling and command != ScheduleCommand.LEVEL:
                raise MspError(
                    ErrorCode.INVALID_REQUEST,
                    "clear_existing_leveling is only valid with the level command",
                )
            self._activate_session(session)
            if command == ScheduleCommand.CALCULATE:
                result = self._invoke(app.CalculateProject)
                self._require_schedule_success(result, "CalculateProject")
            elif command == ScheduleCommand.LEVEL:
                if options.clear_existing_leveling:
                    result = self._invoke(lambda: app.LevelingClear(True))
                    self._require_schedule_success(result, "LevelingClear")
                result = self._invoke(lambda: app.LevelNow(True))
                self._require_schedule_success(result, "LevelNow")
            elif command == ScheduleCommand.CLEAR_LEVELING:
                result = self._invoke(lambda: app.LevelingClear(True))
                self._require_schedule_success(result, "LevelingClear")
            elif command == ScheduleCommand.RESCHEDULE:
                when = options.reschedule_uncompleted_work_to or options.status_date
                if when is None:
                    raise MspError(ErrorCode.INVALID_REQUEST, "reschedule requires a Project-local date")
                result = self._invoke(lambda: app.UpdateProject(True, when, 2))
                self._require_schedule_success(result, "UpdateProject")
            else:  # pragma: no cover - enum exhaustiveness guard
                raise MspError(ErrorCode.UNSUPPORTED_OPERATION, "Unsupported native schedule command")
            after = self._state_on_sta(session)
            return {
                "command": command.value,
                "state_before": before.model_dump(mode="json"),
                "state_after": after.model_dump(mode="json"),
                "native_authority": True,
            }

        return self._existing_call(execute)

    def update_status(
        self,
        project: ProjectRef,
        updates: tuple[StatusOperation, ...],
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]:
        def execute() -> dict[str, Any]:
            session = self._session(project)
            self._require_expected_on_sta(session, expected_state)
            resolved: list[tuple[StatusOperation, Any | None]] = []
            timephased_keys: set[tuple[int, date]] = set()
            for item in updates:
                native = None
                if isinstance(item, TaskProgressUpdate):
                    native = self._resolve_native(session.project, item.task)
                elif isinstance(item, TimephasedWorkUpdate):
                    native = self._resolve_native(session.project, item.assignment)
                    key = (int(native.UniqueID), item.date.date())
                    if key in timephased_keys:
                        raise MspError(
                            ErrorCode.INVALID_REQUEST,
                            "Status batch contains duplicate daily actual work for one assignment",
                            details={"assignment_unique_id": key[0], "date": key[1].isoformat()},
                        )
                    timephased_keys.add(key)
                resolved.append((item, native))
            before = self._state_on_sta(session)
            if before != expected_state:
                raise MspError(ErrorCode.STALE_STATE, "Microsoft Project changed during status validation")
            app = session.app
            self._activate_session(session)
            self._invoke(lambda: app.OpenUndoTransaction("Microsoft Project MCP: status"))
            mutation_possible = False
            undo_open = True
            try:
                for item, task in resolved:
                    mutation_possible = True
                    if isinstance(item, SetStatusDate):
                        session.project.StatusDate = item.status_date
                    elif isinstance(item, TaskProgressUpdate):
                        assert task is not None
                        self._set_task_progress(task, item)
                    elif isinstance(item, TimephasedWorkUpdate):
                        assert task is not None
                        self._set_timephased_actual_work(task, item)
                self._activate_session(session)
                self._require_com_success(
                    self._invoke(app.CalculateProject),
                    "CalculateProject",
                    mismatch=True,
                )
                self._activate_session(session)
                self._invoke(app.CloseUndoTransaction)
                undo_open = False
                observed = self._verify_status_on_sta(session, resolved)
                after = self._state_on_sta(session)
            except Exception as exc:
                if undo_open:
                    try:
                        self._activate_session(session)
                        self._invoke(app.CloseUndoTransaction)
                    except Exception:
                        pass
                if mutation_possible:
                    try:
                        self._activate_session(session)
                        self._invoke(app.Undo)
                        if self._state_on_sta(session) == before:
                            code = (
                                ErrorCode.VERIFICATION_FAILED
                                if isinstance(exc, _RereadMismatch)
                                else ErrorCode.WRITE_ROLLED_BACK
                            )
                            raise MspError(
                                code,
                                "Status update failed and was restored by Undo",
                                retryable=True,
                            ) from exc
                    except MspError:
                        raise
                    except Exception:
                        pass
                    raise BackendExecutionError(
                        "Status update failed and rollback state is uncertain",
                        dispatch_state=DispatchState.MAY_HAVE_DISPATCHED,
                    ) from exc
                raise
            return {
                "updated": len(updates),
                "observed": observed,
                "state_before": before.model_dump(mode="json"),
                "state": after.model_dump(mode="json"),
            }

        return self._existing_call(execute)

    def analyze(self, project: ProjectRef, analysis: AnalysisKind, baseline: int | None) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            if baseline not in (None, 0):
                raise MspError(
                    ErrorCode.UNSUPPORTED_OPERATION,
                    "Live analysis currently supports the primary baseline (0) only",
                )
            session = self._session(project)
            tasks = self._tasks(session.project)
            resources = self._resources(session.project)
            assignments = self._assignments(session.project)
            if analysis == AnalysisKind.CRITICAL_PATH:
                rows = [item for item in tasks if item["critical"]]
            elif analysis == AnalysisKind.CONSTRAINTS:
                rows = [item for item in tasks if item["constraint_type"] not in (None, 0)]
            elif analysis == AnalysisKind.SLACK:
                rows = [{"ref": item["ref"], "total_slack_minutes": item["total_slack_minutes"]} for item in tasks]
            elif analysis == AnalysisKind.OVERALLOCATIONS:
                rows = [item for item in resources if item["overallocated"]]
            elif analysis == AnalysisKind.VARIANCE:
                rows = [item for item in tasks if item["cost_variance"] or item["finish_variance_minutes"]]
            elif analysis == AnalysisKind.EARNED_VALUE:
                rows = [
                    {key: item[key] for key in ("ref", "bcws", "bcwp", "acwp", "cost_variance", "schedule_variance")}
                    for item in tasks
                ]
            elif analysis == AnalysisKind.CHANGE_IMPACT:
                rows = [{"task_count": len(tasks), "resource_count": len(resources), "assignment_count": len(assignments)}]
            else:
                rows = [
                    {
                        "task_count": len(tasks),
                        "critical_count": sum(bool(item["critical"]) for item in tasks),
                        "constraint_count": sum(item["constraint_type"] not in (None, 0) for item in tasks),
                        "overallocated_resource_count": sum(bool(item["overallocated"]) for item in resources),
                    }
                ]
            return {"analysis": analysis.value, "baseline": baseline, "items": rows, "native_authority": True}

        return self._existing_call(read)

    def export(
        self,
        project: ProjectRef,
        format: str,
        destination: str,
        options: ExportOptions,
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]:
        def write() -> dict[str, Any]:
            session = self._session(project)
            self._require_expected_on_sta(session, expected_state)
            target = self._validated_export_target(format, destination, options.overwrite)
            self._require_expected_on_sta(session, expected_state)
            if format == "pdf":
                self._invoke(lambda: session.project.ExportAsFixedFormat(str(target), 0))
            elif format == "mpp":
                source = self._normalized_full_name(session.project)
                if not source or self._dirty(session.project):
                    raise MspError(ErrorCode.INVALID_REQUEST, "MPP export requires a saved, clean source project")
                if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(target)):
                    raise MspError(ErrorCode.INVALID_REQUEST, "MPP export destination must differ from the source")
                shutil.copy2(source, target)
            else:
                raise MspError(ErrorCode.UNSUPPORTED_OPERATION, f"Live {format.upper()} export is not supported")
            if not os.path.isfile(target) or os.path.getsize(target) <= 0:
                raise BackendExecutionError(
                    "Microsoft Project export did not produce a non-empty file",
                    dispatch_state=DispatchState.MAY_HAVE_DISPATCHED,
                )
            return {"format": format, "destination": str(target), "size_bytes": os.path.getsize(target), "written": True}

        return self._existing_call(write)

    def plan_atomicity(self, request_family: str, operations: tuple[Operation, ...] = ()) -> Atomicity:
        if request_family in {"apply", "status"}:
            return Atomicity.UNDO_ATOMIC
        if request_family in {"schedule", "save", "close"}:
            return Atomicity.CHECKPOINTED
        return Atomicity.NON_ATOMIC

    def shutdown(self) -> None:
        if self._sta.state in {StaHostState.NEW, StaHostState.STOPPED}:
            self._sta.shutdown()
            return

        def release() -> None:
            dirty = [
                session
                for session in self._sessions.values()
                if session.ownership == Ownership.SERVER_OWNED and self._dirty(session.project)
            ]
            if dirty:
                raise MspError(ErrorCode.INVALID_REQUEST, "Cannot shut down with dirty server-owned projects")
            for session in list(self._sessions.values()):
                if session.ownership == Ownership.SERVER_OWNED:
                    self._activate_session(session)
                    closed = self._invoke(lambda app=session.app: app.FileCloseEx(0))
                    if closed is False:
                        raise MspError(
                            ErrorCode.INVALID_REQUEST,
                            "Microsoft Project refused or cancelled shutdown close",
                        )
                    self._sessions.pop(session.ref.session_id, None)
            self._sessions.clear()
            if self._server_app is not None:
                self._invoke(self._server_app.Quit)
                self._server_app = None
            self._factory = None

        self._existing_call(release)
        self._sta.shutdown()

    def _require_expected_on_sta(self, session: _LiveSession, expected: ProjectState) -> None:
        actual = self._state_on_sta(session)
        if actual != expected:
            raise MspError(
                ErrorCode.STALE_STATE,
                "Microsoft Project changed before the write reached its STA dispatch boundary",
                details={"expected": expected.token, "actual": actual.token},
            )

    def _resolve_native(
        self,
        project: Any,
        ref: ObjectRef,
        local: dict[tuple[ObjectKind, str], Any] | None = None,
    ) -> Any:
        if ref.client_ref is not None:
            if local is None or (ref.kind, ref.client_ref) not in local:
                raise MspError(ErrorCode.INVALID_REQUEST, "Reference does not name an earlier create in this batch")
            return local[(ref.kind, ref.client_ref)]
        collections = {
            ObjectKind.TASK: "Tasks",
            ObjectKind.RESOURCE: "Resources",
        }
        if ref.kind == ObjectKind.CALENDAR:
            collection = self._calendar_collection(project)
        elif ref.kind == ObjectKind.ASSIGNMENT:
            collection = self._assignment_objects(project)
        elif ref.kind in collections:
            collection = self._read(project, collections[ref.kind], ())
        else:
            raise MspError(ErrorCode.INVALID_REQUEST, "Unsupported native reference kind")
        for native in self._iter(collection):
            if ref.unique_id is not None and int(self._read(native, "UniqueID", 0)) == ref.unique_id:
                return native
            if ref.guid is not None:
                guid = self._text(self._read(native, "GUID", self._read(native, "Guid", "")))
                if guid == ref.guid:
                    return native
        raise MspError(
            ErrorCode.INVALID_REQUEST,
            "Object reference could not be resolved",
            details={"ref": ref.model_dump(mode="json")},
        )

    def _validate_operations_on_sta(self, session: _LiveSession, operations: tuple[Operation, ...]) -> None:
        unsupported_types = (MoveTask,)
        earlier: set[tuple[ObjectKind, str]] = set()
        created_resource_types: dict[str, ResourceType] = {}
        created_assignment_resource_types: dict[str, ResourceType] = {}
        task_parents: dict[str, str | None] = {}
        for native_task in self._iter(self._read(session.project, "Tasks", ())):
            key = f"uid:{int(native_task.UniqueID)}"
            native_parent = self._read(native_task, "OutlineParent", None)
            task_parents[key] = (
                f"uid:{int(native_parent.UniqueID)}" if native_parent is not None else None
            )
        dependency_edges: set[tuple[str, str]] = {
            (f"uid:{left}", f"uid:{right}") for left, right in self._dependency_edges_on_sta(session.project)
        }

        def task_key(ref: ObjectRef) -> str:
            if ref.client_ref is not None:
                return f"client:{ref.client_ref}"
            native = self._resolve_native(session.project, ref)
            return f"uid:{int(native.UniqueID)}"

        def check(ref: ObjectRef) -> None:
            if ref.client_ref is not None:
                if (ref.kind, ref.client_ref) not in earlier:
                    raise MspError(ErrorCode.INVALID_REQUEST, "Batch-local references must target an earlier create")
            else:
                self._resolve_native(session.project, ref)

        for operation in operations:
            if isinstance(operation, unsupported_types):
                raise MspError(ErrorCode.UNSUPPORTED_OPERATION, f"Live {operation.op} is not safely supported")
            if isinstance(operation, CreateTask):
                for ref in (operation.parent, operation.after, operation.calendar):
                    if ref is not None:
                        check(ref)
                parent_key = task_key(operation.parent) if operation.parent is not None else None
                after_key = task_key(operation.after) if operation.after is not None else None
                if parent_key is not None and after_key == parent_key:
                    raise MspError(ErrorCode.INVALID_REQUEST, "A task cannot be placed after its requested parent")
                if after_key is not None:
                    inherited_parent = task_parents[after_key]
                    if parent_key is not None and inherited_parent != parent_key:
                        raise MspError(
                            ErrorCode.INVALID_REQUEST,
                            "The after task must already be a direct child of the requested parent",
                        )
                    if parent_key is None:
                        parent_key = inherited_parent
                earlier.add((ObjectKind.TASK, operation.client_ref))
                task_parents[f"client:{operation.client_ref}"] = parent_key
            elif isinstance(operation, CreateResource):
                if operation.base_calendar is not None:
                    check(operation.base_calendar)
                earlier.add((ObjectKind.RESOURCE, operation.client_ref))
                created_resource_types[operation.client_ref] = operation.resource_type
            elif isinstance(operation, CreateCalendar):
                if operation.base_calendar is not None:
                    check(operation.base_calendar)
                earlier.add((ObjectKind.CALENDAR, operation.client_ref))
            elif isinstance(operation, (UpdateCalendar, DeleteCalendar)):
                check(operation.calendar)
            elif isinstance(operation, CreateAssignment):
                check(operation.task)
                check(operation.resource)
                if operation.resource.client_ref is not None:
                    resource_type = created_resource_types[operation.resource.client_ref]
                else:
                    native_resource = self._resolve_native(session.project, operation.resource)
                    resource_type = ResourceType(
                        self._mapped_enum(native_resource.Type, _RESOURCE_TYPE_FROM_NATIVE)
                    )
                if resource_type == ResourceType.COST:
                    if (
                        operation.cost is None
                        or operation.work_minutes is not None
                        or operation.material_units is not None
                        or operation.units_percent != Decimal("100")
                        or operation.cost_rate_table != "A"
                    ):
                        raise MspError(
                            ErrorCode.INVALID_REQUEST,
                            "Cost-resource assignments require cost and do not accept units, work, or rate-table fields",
                        )
                elif resource_type == ResourceType.MATERIAL:
                    if (
                        operation.material_units is None
                        or operation.units_percent != Decimal("100")
                        or operation.work_minutes is not None
                        or operation.cost is not None
                    ):
                        raise MspError(
                            ErrorCode.INVALID_REQUEST,
                            "Material assignments require material_units and do not accept percentage units, work, or cost",
                        )
                elif operation.cost is not None or operation.material_units is not None:
                    raise MspError(
                        ErrorCode.INVALID_REQUEST,
                        "Cost and material_units require matching resource types",
                    )
                earlier.add((ObjectKind.ASSIGNMENT, operation.client_ref))
                created_assignment_resource_types[operation.client_ref] = resource_type
            elif isinstance(operation, (UpdateTask, DeleteTask)):
                check(operation.task)
                if isinstance(operation, UpdateTask) and operation.calendar is not None:
                    check(operation.calendar)
                if isinstance(operation, DeleteTask):
                    target_key = task_key(operation.task)
                    native_summary = False
                    if operation.task.client_ref is None:
                        native_task = self._resolve_native(session.project, operation.task)
                        native_summary = bool(self._read(native_task, "Summary", False))
                    if (
                        native_summary or any(parent == target_key for parent in task_parents.values())
                    ) and not operation.recursive:
                        raise MspError(
                            ErrorCode.UNSUPPORTED_OPERATION,
                            "Deleting a summary task requires recursive=true",
                        )
                    if operation.recursive and operation.task.client_ref is None:
                        native_tasks = self._iter(self._read(session.project, "Tasks", ()))
                        target_index = next(
                            index
                            for index, item in enumerate(native_tasks)
                            if int(item.UniqueID) == int(native_task.UniqueID)
                        )
                        target_level = int(
                            self._number(self._read(native_task, "OutlineLevel", 1), 1)
                        )
                        subtree = [native_task]
                        for child in native_tasks[target_index + 1 :]:
                            child_level = int(
                                self._number(self._read(child, "OutlineLevel", 1), 1)
                            )
                            if child_level <= target_level:
                                break
                            subtree.append(child)
                        if any(
                            bool(self._read(item, "ExternalTask", False))
                            or bool(self._read(item, "Subproject", False))
                            for item in subtree
                        ):
                            raise MspError(
                                ErrorCode.UNSUPPORTED_OPERATION,
                                "Recursive deletion cannot cross external-task or subproject boundaries",
                            )
            elif isinstance(operation, (UpdateResource, DeleteResource)):
                check(operation.resource)
                if isinstance(operation, UpdateResource):
                    if operation.base_calendar is not None:
                        check(operation.base_calendar)
                    if operation.resource.client_ref is not None:
                        resource_type = created_resource_types[operation.resource.client_ref]
                    else:
                        native = self._resolve_native(session.project, operation.resource)
                        resource_type = ResourceType(
                            self._mapped_enum(native.Type, _RESOURCE_TYPE_FROM_NATIVE)
                        )
                    if resource_type != ResourceType.WORK and operation.max_units_percent is not None:
                        raise MspError(
                            ErrorCode.INVALID_REQUEST,
                            "max_units_percent is only valid for work resources",
                        )
                    if resource_type != ResourceType.WORK and operation.overtime_rate_per_hour is not None:
                        raise MspError(
                            ErrorCode.INVALID_REQUEST,
                            "overtime_rate_per_hour is only valid for work resources",
                        )
                    if resource_type == ResourceType.COST and any(
                        value is not None
                        for value in (
                            operation.standard_rate,
                            operation.cost_per_use,
                            operation.material_label,
                        )
                    ):
                        raise MspError(
                            ErrorCode.INVALID_REQUEST,
                            "Cost resources do not accept rate, per-use, or material fields",
                        )
                    if resource_type != ResourceType.MATERIAL and operation.material_label is not None:
                        raise MspError(
                            ErrorCode.INVALID_REQUEST,
                            "material_label is only valid for material resources",
                        )
                    if resource_type != ResourceType.WORK and operation.base_calendar is not None:
                        raise MspError(
                            ErrorCode.INVALID_REQUEST,
                            "base_calendar is only valid for work resources",
                        )
            elif isinstance(operation, (UpdateAssignment, DeleteAssignment)):
                check(operation.assignment)
                if isinstance(operation, UpdateAssignment):
                    if operation.assignment.client_ref is not None:
                        resource_type = created_assignment_resource_types[operation.assignment.client_ref]
                    else:
                        native_assignment = self._resolve_native(session.project, operation.assignment)
                        resource_type = ResourceType(
                            self._mapped_enum(native_assignment.Resource.Type, _RESOURCE_TYPE_FROM_NATIVE)
                        )
                    if resource_type == ResourceType.COST:
                        if operation.cost is None or any(
                            value is not None
                            for value in (
                                operation.units_percent,
                                operation.material_units,
                                operation.work_minutes,
                                operation.cost_rate_table,
                            )
                        ):
                            raise MspError(
                                ErrorCode.INVALID_REQUEST,
                                "Cost-resource assignments may update only cost",
                            )
                    elif resource_type == ResourceType.MATERIAL:
                        if (
                            operation.material_units is None
                            and operation.cost_rate_table is None
                        ) or any(
                            value is not None
                            for value in (
                                operation.units_percent,
                                operation.work_minutes,
                                operation.cost,
                            )
                        ):
                            raise MspError(
                                ErrorCode.INVALID_REQUEST,
                                "Material assignments may update only material_units or cost_rate_table",
                            )
                    elif operation.cost is not None or operation.material_units is not None:
                        raise MspError(
                            ErrorCode.INVALID_REQUEST,
                            "Cost and material_units require matching resource types",
                        )
            elif isinstance(operation, (AddDependency, RemoveDependency)):
                check(operation.predecessor)
                check(operation.successor)
                edge = (task_key(operation.predecessor), task_key(operation.successor))
                if isinstance(operation, AddDependency):
                    dependency_edges.add(edge)
                else:
                    dependency_edges.discard(edge)
            elif isinstance(operation, UpdateProjectProperties):
                if operation.calendar is not None:
                    check(operation.calendar)

        graph: dict[str, set[str]] = {}
        for left, right in dependency_edges:
            graph.setdefault(left, set()).add(right)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise MspError(ErrorCode.DEPENDENCY_CYCLE, "Dependency operation would create a cycle")
            if node in visited:
                return
            visiting.add(node)
            for successor in graph.get(node, ()):
                visit(successor)
            visiting.remove(node)
            visited.add(node)

        for node in tuple(graph):
            visit(node)

    def _dependency_edges_on_sta(self, project: Any) -> tuple[tuple[int, int], ...]:
        return tuple(
            (item["predecessor"]["unique_id"], item["successor"]["unique_id"])
            for item in self._dependencies(project)
        )

    def _task_boundary_after_subtree(self, project: Any, anchor: Any) -> tuple[Any | None, Any]:
        tasks = self._iter(self._read(project, "Tasks", ()))
        anchor_uid = int(anchor.UniqueID)
        anchor_index = next(
            (index for index, task in enumerate(tasks) if int(task.UniqueID) == anchor_uid),
            None,
        )
        if anchor_index is None:
            raise MspError(ErrorCode.INVALID_REQUEST, "Task placement anchor is no longer in the project")
        anchor_level = int(self._number(self._read(anchor, "OutlineLevel", 1), 1))
        tail = anchor
        for task in tasks[anchor_index + 1 :]:
            level = int(self._number(self._read(task, "OutlineLevel", 1), 1))
            if level <= anchor_level:
                return task, tail
            tail = task
        return None, tail

    def _create_task_on_sta(
        self,
        project: Any,
        operation: CreateTask,
        local: dict[tuple[ObjectKind, str], Any],
    ) -> _CreatedTaskPlacement:
        parent = self._resolve_native(project, operation.parent, local) if operation.parent is not None else None
        after = self._resolve_native(project, operation.after, local) if operation.after is not None else None
        if parent is None and after is not None:
            parent = self._read(after, "OutlineParent", None)
        anchor = after if after is not None else parent
        if anchor is None:
            before_task, subtree_tail = None, None
        else:
            before_task, subtree_tail = self._task_boundary_after_subtree(project, anchor)
        if before_task is None:
            task = self._invoke(lambda: project.Tasks.Add(operation.name))
        else:
            before_id = int(before_task.ID)
            # Tasks.Add's documented Before argument is the insertion row.
            task = self._invoke(lambda: project.Tasks.Add(operation.name, before_id))

        target_level = (
            int(self._number(self._read(parent, "OutlineLevel", 1), 1)) + 1
            if parent is not None
            else 1
        )
        current_level = int(self._number(self._read(task, "OutlineLevel", 1), 1))
        while current_level > target_level:
            self._invoke(task.OutlineOutdent)
            next_level = int(self._number(self._read(task, "OutlineLevel", 1), 1))
            if next_level >= current_level:
                raise _RereadMismatch("Task.OutlineOutdent did not reduce the outline level")
            current_level = next_level
        while current_level < target_level:
            # Task.OutlineIndent is object-scoped and does not use selection/active cells.
            self._invoke(task.OutlineIndent)
            next_level = int(self._number(self._read(task, "OutlineLevel", 1), 1))
            if next_level <= current_level:
                raise _RereadMismatch("Task.OutlineIndent did not advance the outline level")
            current_level = next_level

        return _CreatedTaskPlacement(
            task=task,
            before_unique_id=int(before_task.UniqueID) if before_task is not None else None,
            subtree_tail_unique_id=(
                int(subtree_tail.UniqueID) if subtree_tail is not None else None
            ),
            parent_unique_id=int(parent.UniqueID) if parent is not None else None,
            after_unique_id=int(after.UniqueID) if after is not None else None,
        )

    def _apply_task_planning_fields(
        self,
        project: Any,
        task: Any,
        operation: CreateTask | UpdateTask,
        local: dict[tuple[ObjectKind, str], Any],
    ) -> None:
        if operation.task_type is not None:
            task.Type = _TASK_TYPE_TO_NATIVE[operation.task_type.value]
        for public_name, native_name in (
            ("effort_driven", "EffortDriven"),
            ("manual", "Manual"),
            ("priority", "Priority"),
            ("notes", "Notes"),
        ):
            value = getattr(operation, public_name)
            if value is not None:
                setattr(task, native_name, value)
        if operation.calendar is not None:
            calendar = self._resolve_native(project, operation.calendar, local)
            task.Calendar = self._text(calendar.Name)
        elif isinstance(operation, UpdateTask) and operation.clear_calendar:
            # An empty string selects Project's localized "None" calendar.
            task.Calendar = ""
        if operation.ignore_resource_calendar is not None:
            task.IgnoreResourceCalendar = operation.ignore_resource_calendar
        if operation.constraint_type is not None:
            task.ConstraintType = _TASK_CONSTRAINT_TO_NATIVE[operation.constraint_type.value]
        if operation.constraint_date is not None:
            task.ConstraintDate = operation.constraint_date
        if operation.deadline is not None:
            task.Deadline = operation.deadline
        elif isinstance(operation, UpdateTask) and operation.clear_deadline:
            # Empty text clears the value without relying on localized NA/NV text.
            task.Deadline = ""

    def _apply_resource_details(
        self,
        project: Any,
        resource: Any,
        operation: CreateResource | UpdateResource,
        local: dict[tuple[ObjectKind, str], Any],
    ) -> None:
        if operation.cost_accrual is not None:
            resource.AccrueAt = _COST_ACCRUAL_TO_NATIVE[operation.cost_accrual.value]
        for public_name, native_name in (
            ("initials", "Initials"),
            ("group", "Group"),
            ("code", "Code"),
            ("email", "EMailAddress"),
            ("notes", "Notes"),
        ):
            value = getattr(operation, public_name)
            if value is not None:
                setattr(resource, native_name, value)
        if operation.base_calendar is not None:
            calendar = self._resolve_native(project, operation.base_calendar, local)
            resource.BaseCalendar = self._text(calendar.Name)

    def _apply_one_on_sta(
        self,
        session: _LiveSession,
        operation: Operation,
        local: dict[tuple[ObjectKind, str], Any],
    ) -> Any:
        project = session.project
        if isinstance(operation, CreateTask):
            placement = self._create_task_on_sta(project, operation, local)
            task = placement.task
            task.Duration = operation.duration_minutes
            task.Milestone = operation.milestone
            task.FixedCost = float(operation.fixed_cost)
            task.FixedCostAccrual = _COST_ACCRUAL_TO_NATIVE[operation.cost_accrual.value]
            self._apply_task_planning_fields(project, task, operation, local)
            local[(ObjectKind.TASK, operation.client_ref)] = task
            return placement
        if isinstance(operation, UpdateTask):
            task = self._resolve_native(project, operation.task, local)
            if operation.name is not None:
                task.Name = operation.name
            if operation.duration_minutes is not None:
                task.Duration = operation.duration_minutes
            if operation.fixed_cost is not None:
                task.FixedCost = float(operation.fixed_cost)
            if operation.cost_accrual is not None:
                task.FixedCostAccrual = _COST_ACCRUAL_TO_NATIVE[operation.cost_accrual.value]
            self._apply_task_planning_fields(project, task, operation, local)
            return task
        if isinstance(operation, DeleteTask):
            task = self._resolve_native(project, operation.task, local)
            uid = int(task.UniqueID)
            deleted_uids = [uid]
            if operation.recursive:
                tasks = self._iter(self._read(project, "Tasks", ()))
                index = next(index for index, item in enumerate(tasks) if int(item.UniqueID) == uid)
                level = int(self._number(self._read(task, "OutlineLevel", 1), 1))
                for child in tasks[index + 1 :]:
                    child_level = int(self._number(self._read(child, "OutlineLevel", 1), 1))
                    if child_level <= level:
                        break
                    deleted_uids.append(int(child.UniqueID))
            self._invoke(task.Delete)
            return (ObjectKind.TASK, tuple(deleted_uids))
        if isinstance(operation, CreateResource):
            resource = self._invoke(lambda: project.Resources.Add(operation.name))
            resource.Type = _RESOURCE_TYPE_TO_NATIVE[operation.resource_type.value]
            if operation.resource_type == ResourceType.WORK:
                resource.MaxUnits = float(operation.max_units_percent) / 100.0
                resource.StandardRate = float(operation.standard_rate)
                resource.OvertimeRate = float(operation.overtime_rate_per_hour)
                resource.CostPerUse = float(operation.cost_per_use)
            elif operation.resource_type == ResourceType.MATERIAL:
                resource.StandardRate = float(operation.standard_rate)
                resource.CostPerUse = float(operation.cost_per_use)
                if operation.material_label is not None:
                    resource.MaterialLabel = operation.material_label
            self._apply_resource_details(project, resource, operation, local)
            local[(ObjectKind.RESOURCE, operation.client_ref)] = resource
            return resource
        if isinstance(operation, UpdateResource):
            resource = self._resolve_native(project, operation.resource, local)
            resource_type = ResourceType(self._mapped_enum(resource.Type, _RESOURCE_TYPE_FROM_NATIVE))
            if operation.name is not None:
                resource.Name = operation.name
            if operation.max_units_percent is not None:
                resource.MaxUnits = float(operation.max_units_percent) / 100.0
            rate_fields = [
                ("StandardRate", operation.standard_rate),
                ("CostPerUse", operation.cost_per_use),
            ]
            if resource_type == ResourceType.WORK:
                rate_fields.append(("OvertimeRate", operation.overtime_rate_per_hour))
            if resource_type == ResourceType.MATERIAL:
                rate_fields.append(("MaterialLabel", operation.material_label))
            for field, value in rate_fields:
                if value is not None:
                    setattr(resource, field, float(value) if isinstance(value, Decimal) else value)
            self._apply_resource_details(project, resource, operation, local)
            return resource
        if isinstance(operation, DeleteResource):
            resource = self._resolve_native(project, operation.resource, local)
            uid = int(resource.UniqueID)
            self._invoke(resource.Delete)
            return (ObjectKind.RESOURCE, uid)
        if isinstance(operation, CreateAssignment):
            task = self._resolve_native(project, operation.task, local)
            resource = self._resolve_native(project, operation.resource, local)
            # Assignments collections belong to tasks or resources, not Project. The
            # transient IDs are consumed only here after resolving stable UniqueID refs.
            assignment = self._invoke(
                lambda: task.Assignments.Add(int(task.ID), int(resource.ID))
            )
            resource_type = ResourceType(self._mapped_enum(resource.Type, _RESOURCE_TYPE_FROM_NATIVE))
            if resource_type == ResourceType.COST:
                assignment.Cost = float(operation.cost)
            elif resource_type == ResourceType.MATERIAL:
                assignment.Units = float(operation.material_units)
                assignment.CostRateTable = _COST_RATE_TO_NATIVE[operation.cost_rate_table]
            else:
                # Assignment.Units is always decimal; Assignments.Add's optional
                # Units argument changes meaning with a user display preference.
                assignment.Units = float(operation.units_percent) / 100.0
                if operation.work_minutes is not None:
                    assignment.Work = operation.work_minutes
                assignment.CostRateTable = _COST_RATE_TO_NATIVE[operation.cost_rate_table]
            local[(ObjectKind.ASSIGNMENT, operation.client_ref)] = assignment
            return assignment
        if isinstance(operation, UpdateAssignment):
            assignment = self._resolve_native(project, operation.assignment, local)
            if operation.cost is not None:
                assignment.Cost = float(operation.cost)
            if operation.material_units is not None:
                assignment.Units = float(operation.material_units)
            if operation.units_percent is not None:
                assignment.Units = float(operation.units_percent) / 100.0
            if operation.work_minutes is not None:
                assignment.Work = operation.work_minutes
            if operation.cost_rate_table is not None:
                assignment.CostRateTable = _COST_RATE_TO_NATIVE[operation.cost_rate_table]
            return assignment
        if isinstance(operation, DeleteAssignment):
            assignment = self._resolve_native(project, operation.assignment, local)
            uid = int(assignment.UniqueID)
            self._invoke(assignment.Delete)
            return (ObjectKind.ASSIGNMENT, uid)
        if isinstance(operation, AddDependency):
            predecessor = self._resolve_native(project, operation.predecessor, local)
            successor = self._resolve_native(project, operation.successor, local)
            return self._invoke(
                lambda: successor.TaskDependencies.Add(
                    predecessor,
                    _TASK_LINK_TO_NATIVE[operation.dependency_type],
                    operation.lag_minutes,
                )
            )
        if isinstance(operation, RemoveDependency):
            predecessor = self._resolve_native(project, operation.predecessor, local)
            successor = self._resolve_native(project, operation.successor, local)
            for dependency in self._iter(successor.TaskDependencies):
                if int(dependency.From.UniqueID) == int(predecessor.UniqueID):
                    self._invoke(dependency.Delete)
                    return (int(predecessor.UniqueID), int(successor.UniqueID))
            raise MspError(ErrorCode.INVALID_REQUEST, "Dependency does not exist")
        if isinstance(operation, CreateCalendar):
            self._activate_session(session)
            kwargs: dict[str, Any] = {"Name": operation.name}
            if operation.base_calendar is not None:
                base = self._resolve_native(project, operation.base_calendar, local)
                kwargs["FromName"] = self._text(base.Name)
            self._invoke(lambda: session.app.BaseCalendarCreate(**kwargs))
            calendar = self._calendar_by_name(project, operation.name)
            self._write_calendar_details(session, calendar, operation.weekly, operation.exceptions)
            local[(ObjectKind.CALENDAR, operation.client_ref)] = calendar
            return calendar
        if isinstance(operation, UpdateCalendar):
            calendar = self._resolve_native(project, operation.calendar, local)
            self._activate_session(session)
            if operation.name is not None:
                old_name = self._text(calendar.Name)
                self._invoke(
                    lambda: session.app.BaseCalendarRename(FromName=old_name, ToName=operation.name)
                )
                calendar = self._calendar_by_name(project, operation.name)
            if operation.weekly is not None or operation.exceptions is not None:
                self._write_calendar_details(
                    session,
                    calendar,
                    operation.weekly,
                    operation.exceptions,
                    replace_exceptions=operation.exceptions is not None,
                )
            return calendar
        if isinstance(operation, DeleteCalendar):
            calendar = self._resolve_native(project, operation.calendar, local)
            guid = self._text(self._read(calendar, "GUID", self._read(calendar, "Guid", "")))
            name = self._text(calendar.Name)
            self._activate_session(session)
            self._invoke(lambda: session.app.BaseCalendarDelete(Name=name))
            return (ObjectKind.CALENDAR, guid)
        if isinstance(operation, UpdateProjectProperties):
            self._activate_session(session)
            calendar_name = None
            schedule_from = operation.schedule_from
            if schedule_from is None and operation.project_start is not None:
                schedule_from = ScheduleFrom.START
            elif schedule_from is None and operation.project_finish is not None:
                schedule_from = ScheduleFrom.FINISH
            if operation.calendar is not None:
                calendar = self._resolve_native(project, operation.calendar, local)
                calendar_name = self._text(calendar.Name)
            values = {
                key: value
                for key, value in {
                    "Title": operation.title,
                    "Subject": operation.subject,
                    "Author": operation.author,
                    "Company": operation.company,
                    "Manager": operation.manager,
                    "Keywords": operation.keywords,
                    "Comments": operation.comments,
                    "Start": operation.project_start,
                    "Finish": operation.project_finish,
                    "ScheduleFrom": (
                        _SCHEDULE_TO_NATIVE[schedule_from.value]
                        if schedule_from is not None
                        else None
                    ),
                    "CurrentDate": operation.current_date,
                    "Calendar": calendar_name,
                    "Priority": operation.priority,
                }.items()
                if value is not None
            }
            if values:
                project_name = self._text(self._read(project, "FullName", "")) or self._text(project.Name)
                keys = (
                    "Title", "Subject", "Author", "Company", "Manager", "Keywords", "Comments",
                    "Start", "Finish", "ScheduleFrom", "CurrentDate", "Calendar", "StatusDate", "Priority",
                )
                current = {
                    "Title": self._text(self._read(project, "Title", "")),
                    "Subject": self._text(self._read(project, "Subject", "")),
                    "Author": self._text(self._read(project, "Author", "")),
                    "Company": self._text(self._read(project, "Company", "")),
                    "Manager": self._text(self._read(project, "Manager", "")),
                    "Keywords": self._text(self._read(project, "Keywords", "")),
                    "Comments": self._text(self._read(project, "ProjectNotes", "")),
                    "Start": self._read(project, "ProjectStart", None),
                    "Finish": self._read(project, "ProjectFinish", None),
                    "ScheduleFrom": 1 if bool(self._read(project, "ScheduleFromStart", True)) else 2,
                    "CurrentDate": self._read(project, "CurrentDate", None),
                    "Calendar": self._text(self._read(self._read(project, "Calendar", None), "Name", "")),
                    "StatusDate": self._read(project, "StatusDate", "NA"),
                    "Priority": int(
                        self._number(
                            self._read(self._read(project, "ProjectSummaryTask", None), "Priority", 500),
                            500,
                        )
                    ),
                }
                current.update(values)
                last = max(index for index, key in enumerate(keys) if key in values)
                args = (project_name,) + tuple(current[key] for key in keys[: last + 1])
                self._invoke(lambda: session.app.ProjectSummaryInfo(*args))
            if operation.comments is not None:
                project.ProjectNotes = operation.comments
            direct = {
                "DefaultTaskType": (
                    _TASK_TYPE_TO_NATIVE[operation.default_task_type.value]
                    if operation.default_task_type is not None
                    else None
                ),
                "DefaultEffortDriven": operation.default_effort_driven,
                "NewTasksCreatedAsManual": operation.new_tasks_manual,
                "HonorConstraints": operation.honor_constraints,
                "MultipleCriticalPaths": operation.multiple_critical_paths,
                "HoursPerDay": float(operation.hours_per_day) if operation.hours_per_day is not None else None,
                "HoursPerWeek": float(operation.hours_per_week) if operation.hours_per_week is not None else None,
                "DaysPerMonth": float(operation.days_per_month) if operation.days_per_month is not None else None,
            }
            for name, value in direct.items():
                if value is not None:
                    setattr(project, name, value)
            return project
        if isinstance(operation, SetBaseline):
            self._activate_session(session)
            self._invoke(lambda: session.app.BaselineSave(True, 0, _BASELINE_INTO[operation.baseline]))
            return operation.baseline
        if isinstance(operation, ClearBaseline):
            self._activate_session(session)
            self._invoke(lambda: session.app.BaselineClear(True, _BASELINE_INTO[operation.baseline]))
            return operation.baseline
        raise MspError(ErrorCode.UNSUPPORTED_OPERATION, f"Unsupported live operation: {operation.op}")

    def _calendar_by_name(self, project: Any, name: str) -> Any:
        for calendar in self._iter(self._calendar_collection(project)):
            if self._text(calendar.Name) == name:
                return calendar
        raise _RereadMismatch("Created or renamed calendar was not returned by Project.BaseCalendars")

    def _write_calendar_details(
        self,
        session: _LiveSession,
        calendar: Any,
        weekly: tuple[Any, ...] | None,
        exceptions: tuple[Any, ...] | None,
        *,
        replace_exceptions: bool = False,
    ) -> None:
        name = self._text(calendar.Name)
        if weekly is not None:
            for day in weekly:
                kwargs: dict[str, Any] = {
                    "Name": name,
                    "WeekDay": _WEEKDAY_TO_NATIVE[day.weekday],
                    "Working": bool(day.intervals),
                }
                for index, interval in enumerate(day.intervals, start=1):
                    kwargs[f"From{index}"] = interval.start
                    kwargs[f"To{index}"] = interval.end
                self._activate_session(session)
                self._invoke(lambda kwargs=kwargs: session.app.BaseCalendarEditDays(**kwargs))
        if exceptions is not None:
            native_exceptions = self._read(calendar, "Exceptions", ())
            if replace_exceptions:
                for item in reversed(self._iter(native_exceptions)):
                    self._invoke(item.Delete)
            for exception in exceptions:
                created = self._invoke(
                    lambda exception=exception: native_exceptions.Add(
                        Type=1,
                        Start=exception.start_date,
                        Finish=exception.end_date,
                        Name=exception.name,
                    )
                )
                if exception.working:
                    for index, interval in enumerate(exception.intervals, start=1):
                        shift = self._read(created, f"Shift{index}", None)
                        if shift is None:
                            raise MspError(
                                ErrorCode.UNSUPPORTED_OPERATION,
                                "Calendar exception does not expose documented working shifts",
                            )
                        shift.Start = interval.start
                        shift.Finish = interval.end

    def _verify_operation_on_sta(
        self,
        session: _LiveSession,
        operation: Operation,
        native: Any,
        local: dict[tuple[ObjectKind, str], Any],
    ) -> dict[str, Any]:
        project = session.project
        observed: dict[str, Any]
        if isinstance(operation, (DeleteTask, DeleteResource, DeleteAssignment, DeleteCalendar)):
            kind, identity = native
            identities = identity if isinstance(identity, tuple) else (identity,)
            deleted_refs = []
            for uid in identities:
                try:
                    ref = (
                        ObjectRef(kind=kind, guid=uid)
                        if kind == ObjectKind.CALENDAR
                        else ObjectRef(kind=kind, unique_id=uid)
                    )
                    self._resolve_native(project, ref)
                except MspError:
                    deleted_refs.append(
                        {
                            "kind": kind.value,
                            "guid" if kind == ObjectKind.CALENDAR else "unique_id": uid,
                        }
                    )
                else:
                    raise _RereadMismatch("Deleted object remains present")
            observed = (
                {"deleted_ref": deleted_refs[0]}
                if len(deleted_refs) == 1
                else {"deleted_refs": deleted_refs}
            )
        elif isinstance(operation, (AddDependency, RemoveDependency)):
            pred = self._resolve_native(project, operation.predecessor, local)
            succ = self._resolve_native(project, operation.successor, local)
            matches = [
                item for item in self._dependencies(project)
                if item["predecessor"]["unique_id"] == int(pred.UniqueID)
                and item["successor"]["unique_id"] == int(succ.UniqueID)
            ]
            if isinstance(operation, AddDependency):
                matches = [
                    item for item in matches
                    if item["dependency_type"] == operation.dependency_type
                    and item["lag_minutes"] == operation.lag_minutes
                ]
                if not matches:
                    raise _RereadMismatch("Dependency reread did not match")
            elif matches:
                raise _RereadMismatch("Removed dependency remains present")
            observed = {"dependency_count": len(matches)}
        elif isinstance(operation, UpdateProjectProperties):
            observed = self._project_items(project)[0]
            schedule_from = operation.schedule_from
            if schedule_from is None and operation.project_start is not None:
                schedule_from = ScheduleFrom.START
            elif schedule_from is None and operation.project_finish is not None:
                schedule_from = ScheduleFrom.FINISH
            calendar_ref = None
            if operation.calendar is not None:
                calendar = self._resolve_native(project, operation.calendar, local)
                calendar_ref = self._calendar_ref(calendar)
            expected = {
                "title": operation.title,
                "manager": operation.manager,
                "company": operation.company,
                "subject": operation.subject,
                "author": operation.author,
                "keywords": operation.keywords,
                "comments": operation.comments,
                "project_start": self._date_value(operation.project_start),
                "project_finish": self._date_value(operation.project_finish),
                "schedule_from": schedule_from.value if schedule_from is not None else None,
                "current_date": self._date_value(operation.current_date),
                "calendar_ref": calendar_ref,
                "priority": operation.priority,
                "default_task_type": (
                    operation.default_task_type.value if operation.default_task_type is not None else None
                ),
                "default_effort_driven": operation.default_effort_driven,
                "new_tasks_manual": operation.new_tasks_manual,
                "honor_constraints": operation.honor_constraints,
                "multiple_critical_paths": operation.multiple_critical_paths,
                "hours_per_day": float(operation.hours_per_day) if operation.hours_per_day is not None else None,
                "hours_per_week": float(operation.hours_per_week) if operation.hours_per_week is not None else None,
                "days_per_month": float(operation.days_per_month) if operation.days_per_month is not None else None,
            }
            for key, value in expected.items():
                if value is not None and observed[key] != value:
                    raise _RereadMismatch(
                        f"Project property {key} did not match: expected {value!r}, "
                        f"observed {observed[key]!r}"
                    )
        elif isinstance(operation, (SetBaseline, ClearBaseline)):
            observed = next(item for item in self._baselines(project) if item["baseline"] == operation.baseline)
            expected_set = isinstance(operation, SetBaseline)
            if observed["set"] is not expected_set:
                raise _RereadMismatch("Baseline reread did not match")
        else:
            placement = native if isinstance(operation, CreateTask) else None
            native_object = placement.task if placement is not None else native
            kind = (
                ObjectKind.TASK if isinstance(operation, (CreateTask, UpdateTask))
                else ObjectKind.RESOURCE if isinstance(operation, (CreateResource, UpdateResource))
                else ObjectKind.CALENDAR if isinstance(operation, (CreateCalendar, UpdateCalendar))
                else ObjectKind.ASSIGNMENT
            )
            uid = (
                self._text(self._read(native_object, "GUID", self._read(native_object, "Guid", "")))
                if kind == ObjectKind.CALENDAR
                else int(native_object.UniqueID)
            )
            entity = self._entity_for_kind(kind)
            observed = next(
                item for item in self._projection(project, entity)
                if item["ref"].get("guid" if kind == ObjectKind.CALENDAR else "unique_id") == uid
            )
            checks: dict[str, Any] = {}
            if isinstance(operation, (CreateTask, UpdateTask)):
                calendar_ref = None
                if operation.calendar is not None:
                    calendar_ref = self._calendar_ref(
                        self._resolve_native(project, operation.calendar, local)
                    )
                checks = {
                    "name": operation.name,
                    "duration_minutes": operation.duration_minutes,
                    "fixed_cost": float(operation.fixed_cost) if operation.fixed_cost is not None else None,
                    "cost_accrual": operation.cost_accrual.value if operation.cost_accrual is not None else None,
                    "constraint_type_name": (
                        operation.constraint_type.value if operation.constraint_type is not None else None
                    ),
                    "constraint_date": self._date_value(operation.constraint_date),
                    "deadline": (
                        None if isinstance(operation, UpdateTask) and operation.clear_deadline
                        else self._date_value(operation.deadline)
                    ),
                    "task_type": operation.task_type.value if operation.task_type is not None else None,
                    "effort_driven": operation.effort_driven,
                    "manual": operation.manual,
                    "priority": operation.priority,
                    "notes": operation.notes,
                    "calendar_ref": (
                        None if isinstance(operation, UpdateTask) and operation.clear_calendar
                        else calendar_ref
                    ),
                    "ignore_resource_calendar": operation.ignore_resource_calendar,
                }
            elif isinstance(operation, (CreateResource, UpdateResource)):
                checks = {
                    "name": operation.name,
                    "cost_accrual": operation.cost_accrual.value if operation.cost_accrual is not None else None,
                    "initials": operation.initials,
                    "group": operation.group,
                    "code": operation.code,
                    "email": operation.email,
                    "notes": operation.notes,
                }
                if operation.base_calendar is not None:
                    checks["base_calendar_ref"] = self._calendar_ref(
                        self._resolve_native(project, operation.base_calendar, local)
                    )
                resource_type = (
                    operation.resource_type
                    if isinstance(operation, CreateResource)
                    else ResourceType(observed["resource_type"])
                )
                if isinstance(operation, CreateResource):
                    checks["resource_type"] = resource_type.value
                if resource_type == ResourceType.WORK:
                    checks.update(
                        {
                            "max_units_percent": float(operation.max_units_percent) if operation.max_units_percent is not None else None,
                            "standard_rate": float(operation.standard_rate) if operation.standard_rate is not None else None,
                            "overtime_rate_per_hour": float(operation.overtime_rate_per_hour) if operation.overtime_rate_per_hour is not None else None,
                            "cost_per_use": float(operation.cost_per_use) if operation.cost_per_use is not None else None,
                        }
                    )
                elif resource_type == ResourceType.MATERIAL:
                    checks.update(
                        {
                            "standard_rate": float(operation.standard_rate) if operation.standard_rate is not None else None,
                            "cost_per_use": float(operation.cost_per_use) if operation.cost_per_use is not None else None,
                            "material_label": operation.material_label,
                        }
                    )
            elif isinstance(operation, (CreateAssignment, UpdateAssignment)):
                assignment_resource_type = ResourceType(
                    self._mapped_enum(native_object.Resource.Type, _RESOURCE_TYPE_FROM_NATIVE)
                )
                if assignment_resource_type == ResourceType.COST:
                    checks = {"cost": float(operation.cost) if operation.cost is not None else None}
                elif assignment_resource_type == ResourceType.MATERIAL:
                    checks = {
                        "material_units": float(operation.material_units) if operation.material_units is not None else None,
                        "cost_rate_table": operation.cost_rate_table,
                    }
                else:
                    checks = {
                        "units_percent": float(operation.units_percent) if operation.units_percent is not None else None,
                        "work_minutes": operation.work_minutes,
                        "cost_rate_table": operation.cost_rate_table,
                    }
            elif isinstance(operation, (CreateCalendar, UpdateCalendar)):
                checks = {"name": operation.name}
                if operation.weekly is not None:
                    requested_weekly = [day.model_dump(mode="json") for day in operation.weekly]
                    observed_weekly = [
                        {"weekday": day["weekday"], "intervals": day["intervals"]}
                        for day in observed["weekly"]
                        if day["weekday"] in {item["weekday"] for item in requested_weekly}
                    ]
                    if observed_weekly != requested_weekly:
                        raise _RereadMismatch("Calendar weekly intervals did not match")
                if operation.exceptions is not None:
                    requested_exceptions = [item.model_dump(mode="json") for item in operation.exceptions]
                    if observed["exceptions"] != requested_exceptions:
                        raise _RereadMismatch("Calendar exceptions did not match")
            explicit_none = {
                "deadline"
                if isinstance(operation, UpdateTask) and operation.clear_deadline
                else "",
                "calendar_ref"
                if isinstance(operation, UpdateTask) and operation.clear_calendar
                else "",
            }
            for key, value in checks.items():
                if (value is not None or key in explicit_none) and observed[key] != value:
                    raise _RereadMismatch(
                        f"{operation.op} field {key} did not match: expected {value!r}, "
                        f"observed {observed[key]!r}"
                    )
            if placement is not None:
                reread = self._resolve_native(
                    project,
                    ObjectRef(kind=ObjectKind.TASK, unique_id=uid),
                )
                parent = self._read(reread, "OutlineParent", None)
                parent_uid_value = (
                    int(self._number(self._read(parent, "UniqueID", 0), 0))
                    if parent is not None
                    else 0
                )
                parent_uid = parent_uid_value or None
                if parent_uid != placement.parent_unique_id:
                    raise _RereadMismatch(
                        f"Created task parent did not match for {operation.name}: "
                        f"expected {placement.parent_unique_id}, observed {parent_uid}"
                    )
                ordered = self._iter(self._read(project, "Tasks", ()))
                positions = {int(task.UniqueID): index for index, task in enumerate(ordered)}
                position = positions[uid]
                if (
                    placement.before_unique_id is not None
                    and placement.before_unique_id in positions
                ):
                    before_position = positions[placement.before_unique_id]
                    if position >= before_position:
                        raise _RereadMismatch("Created task crossed its insertion boundary")
                if (
                    placement.subtree_tail_unique_id is not None
                    and placement.subtree_tail_unique_id in positions
                ):
                    tail_position = positions[placement.subtree_tail_unique_id]
                    if position <= tail_position:
                        raise _RereadMismatch("Created task was not placed after the original subtree")
                if placement.after_unique_id is not None:
                    after = self._resolve_native(
                        project,
                        ObjectRef(kind=ObjectKind.TASK, unique_id=placement.after_unique_id),
                    )
                    after_index = next(
                        index
                        for index, task in enumerate(ordered)
                        if int(task.UniqueID) == placement.after_unique_id
                    )
                    after_level = int(self._number(self._read(after, "OutlineLevel", 1), 1))
                    first_after_subtree = next(
                        (
                            index
                            for index in range(after_index + 1, len(ordered))
                            if int(self._number(self._read(ordered[index], "OutlineLevel", 1), 1))
                            <= after_level
                        ),
                        len(ordered),
                    )
                    if first_after_subtree != position:
                        raise _RereadMismatch("Created task was not placed after the requested subtree")
        return {"op": operation.op, "verified": True, "native": observed}

    @staticmethod
    def _set_task_progress(task: Any, update: TaskProgressUpdate) -> None:
        mapping = {
            "percent_complete": "PercentComplete",
            "actual_duration_minutes": "ActualDuration",
            "remaining_duration_minutes": "RemainingDuration",
            "actual_work_minutes": "ActualWork",
            "remaining_work_minutes": "RemainingWork",
            "actual_start": "ActualStart",
            "actual_finish": "ActualFinish",
        }
        for public_name, native_name in mapping.items():
            value = getattr(update, public_name)
            if value is not None:
                setattr(task, native_name, float(value) if isinstance(value, Decimal) else value)

    def _timephased_actual_work_cell(self, assignment: Any, day: datetime) -> Any:
        # Official Project object model: Assignment.TimeScaleData(StartDate,
        # EndDate, Type=10, TimeScaleUnit=4, Count=1), then TimeScaleValue.Value.
        values = self._invoke(
            lambda: assignment.TimeScaleData(
                day,
                day,
                _PJ_ASSIGNMENT_TIMESCALED_ACTUAL_WORK,
                _PJ_TIMESCALE_DAYS,
                1,
            )
        )
        item = getattr(values, "Item", None)
        if callable(item):
            cell = self._invoke(lambda: item(1))
        else:
            cells = self._iter(values)
            cell = cells[0] if cells else None
        if cell is None:
            raise MspError(ErrorCode.UNSUPPORTED_OPERATION, "Project returned no daily timephased work cell")
        return cell

    def _set_timephased_actual_work(self, assignment: Any, update: TimephasedWorkUpdate) -> None:
        cell = self._timephased_actual_work_cell(assignment, update.date)
        cell.Value = update.actual_work_minutes

    def _verify_status_on_sta(
        self,
        session: _LiveSession,
        resolved: list[tuple[StatusOperation, Any | None]],
    ) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        mapping = {
            "percent_complete": "PercentComplete",
            "actual_duration_minutes": "ActualDuration",
            "remaining_duration_minutes": "RemainingDuration",
            "actual_work_minutes": "ActualWork",
            "remaining_work_minutes": "RemainingWork",
            "actual_start": "ActualStart",
            "actual_finish": "ActualFinish",
        }
        for update, task in resolved:
            if isinstance(update, SetStatusDate):
                actual = self._date_value(session.project.StatusDate)
                expected = self._date_value(update.status_date)
                if actual != expected:
                    raise _RereadMismatch("Project status date did not match")
                observations.append({"op": update.op, "status_date": actual})
                continue
            if isinstance(update, TimephasedWorkUpdate):
                assert task is not None
                cell = self._timephased_actual_work_cell(task, update.date)
                actual = int(self._number(self._read(cell, "Value", 0)))
                if actual != update.actual_work_minutes:
                    raise _RereadMismatch("Daily assignment actual work did not match")
                observations.append(
                    {
                        "op": update.op,
                        "native": {
                            "ref": {"kind": "assignment", "unique_id": int(task.UniqueID)},
                            "date": update.date.date().isoformat(),
                            "actual_work_minutes": actual,
                        },
                    }
                )
                continue
            assert isinstance(update, TaskProgressUpdate) and task is not None
            values: dict[str, Any] = {"ref": {"kind": "task", "unique_id": int(task.UniqueID)}}
            for public_name, native_name in mapping.items():
                requested = getattr(update, public_name)
                if requested is None:
                    continue
                raw = self._read(task, native_name, None)
                actual = self._date_value(raw) if isinstance(requested, datetime) else self._number(raw)
                expected = self._date_value(requested) if isinstance(requested, datetime) else float(requested)
                if actual != expected:
                    raise _RereadMismatch(
                        f"Task progress field {public_name} did not match: "
                        f"expected {expected}, observed {actual}"
                    )
                values[public_name] = actual
            observations.append({"op": update.op, "native": values})
        return observations

    @staticmethod
    def _validated_export_target(format: str, destination: str, overwrite: bool) -> Path:
        target = Path(destination)
        if not target.is_absolute():
            raise MspError(ErrorCode.INVALID_REQUEST, "Export destination must be absolute")
        if target.suffix.lower() != f".{format}":
            raise MspError(ErrorCode.INVALID_REQUEST, "Export destination extension does not match format")
        if not target.parent.is_dir():
            raise MspError(ErrorCode.INVALID_REQUEST, "Export destination parent directory does not exist")
        if target.exists() and not overwrite:
            raise MspError(ErrorCode.INVALID_REQUEST, "Export destination already exists and overwrite is false")
        return target

    def _invoke(self, function: Callable[[], Any]) -> Any:
        """Retry only COM's explicit pre-dispatch busy/rejected-call HRESULTs."""
        retryable = {-2147418111, -2147417846}  # RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER
        delay = 0.01
        for attempt in range(4):
            try:
                return function()
            except Exception as exc:
                hresult = getattr(exc, "hresult", None)
                if hresult is None and exc.args and isinstance(exc.args[0], int):
                    hresult = exc.args[0]
                if hresult not in retryable:
                    raise
                if attempt == 3:
                    raise BackendExecutionError(
                        "Microsoft Project remained busy before accepting the COM call",
                        dispatch_state=DispatchState.NOT_DISPATCHED,
                        details={"hresult": hresult, "attempts": attempt + 1},
                    ) from exc
                self._sta.pump_current_thread()
                time_module.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    def _unsupported(self, message: str) -> Any:
        raise MspError(ErrorCode.UNSUPPORTED_OPERATION, message)

    def _activation_call(self, function: Callable[[], Any]) -> Any:
        self._ensure_started()
        return self._host_call(function)

    def _existing_call(self, function: Callable[[], Any]) -> Any:
        if self._sta.state != StaHostState.RUNNING:
            raise MspError(ErrorCode.SESSION_NOT_FOUND, "No live Microsoft Project session is active")
        return self._host_call(function)

    def _ensure_started(self) -> None:
        if self._sta.state == StaHostState.RUNNING:
            return
        with self._start_lock:
            if self._sta.state == StaHostState.RUNNING:
                return
            try:
                self._sta.start()
            except (StaHostClosedError, StaWorkerFailedError, TimeoutError, RuntimeError) as exc:
                raise BackendExecutionError(
                    "Microsoft Project STA worker could not start",
                    dispatch_state=DispatchState.NOT_DISPATCHED,
                    details={"cause": type(exc).__name__},
                ) from exc

    def _host_call(self, function: Callable[[], Any]) -> Any:
        try:
            return self._sta.call(function, timeout=self._call_timeout)
        except (MspError, BackendExecutionError):
            raise
        except StaCallTimeout as exc:
            raise BackendExecutionError(
                "Microsoft Project STA call timed out",
                dispatch_state=DispatchState.MAY_HAVE_DISPATCHED,
            ) from exc
        except (StaHostClosedError, StaWorkerFailedError) as exc:
            raise BackendExecutionError(
                "Microsoft Project STA worker rejected the call",
                dispatch_state=DispatchState.NOT_DISPATCHED,
                details={"cause": type(exc).__name__},
            ) from exc
        except Exception as exc:
            raise BackendExecutionError(
                "Microsoft Project automation failed",
                dispatch_state=DispatchState.MAY_HAVE_DISPATCHED,
                details={"cause": type(exc).__name__},
            ) from exc

    def _automation_factory(self) -> AutomationFactory:
        if self._factory is None:
            self._factory = self._automation_factory_provider()
        return self._factory

    def _server_application(self) -> Any:
        if self._server_app is None:
            self._server_app = self._automation_factory().create_application()
            self._server_app.Visible = self._server_app_visible
            # This Application instance is owned exclusively by the backend.
            # Prevent scheduling conflicts from opening modal wizard/error
            # messages that would otherwise wedge the STA worker.
            self._server_app.DisplayAlerts = False
            self._server_app.DisplayWizardErrors = False
            self._server_app.DisplayWizardScheduling = False
        return self._server_app

    def _bind(self, app: Any, project: Any, ownership: Ownership) -> _LiveSession:
        self._touch_project(project)
        session_id = f"live-session-{self._instance_namespace}-{self._next_session:08d}"
        project_key = f"live-project-{self._instance_namespace}-{self._next_session:08d}"
        self._next_session += 1
        session = _LiveSession(
            ref=ProjectRef(session_id=session_id, project_key=project_key),
            ownership=ownership,
            app=app,
            project=project,
            normalized_full_name=self._normalized_full_name(project),
            native_guid=self._native_guid(project),
        )
        self._sessions[session_id] = session
        return session

    def _session(self, ref: ProjectRef) -> _LiveSession:
        session = self._sessions.get(ref.session_id)
        if session is None:
            raise MspError(ErrorCode.SESSION_NOT_FOUND, "Live project session was not found")
        if session.ref.project_key != ref.project_key:
            raise MspError(ErrorCode.PROJECT_IDENTITY_CHANGED, "Project key does not match the bound session")
        try:
            self._touch_project(session.project)
            full_name = self._normalized_full_name(session.project)
            native_guid = self._native_guid(session.project)
        except MspError:
            raise
        except Exception as exc:
            raise MspError(ErrorCode.PROJECT_CLOSED, "The bound Microsoft Project document is closed") from exc
        if session.normalized_full_name and full_name != session.normalized_full_name:
            raise MspError(ErrorCode.PROJECT_IDENTITY_CHANGED, "The bound project path changed outside this session")
        if session.native_guid and native_guid != session.native_guid:
            raise MspError(ErrorCode.PROJECT_IDENTITY_CHANGED, "The bound native project signature changed")
        return session

    def _public_session(self, session: _LiveSession) -> ProjectSession:
        return ProjectSession(
            project=session.ref,
            ownership=session.ownership,
            name=(
                self._text(self._read(session.project, "Title", "")).strip()
                or self._text(self._read(session.project, "Name", "Untitled"))
            ),
            path=self._text(self._read(session.project, "FullName", "")) or None,
            dirty=self._dirty(session.project),
            state=self._state_on_sta(session),
        )

    def _save_on_sta(self, session: _LiveSession, path: str | None) -> None:
        if path:
            path = self._validated_save_target(path, current=session.normalized_full_name)
            save_as = getattr(session.project, "SaveAs", None)
            if callable(save_as):
                saved = self._invoke(lambda: save_as(path))
            else:
                session.project.Activate()
                saved = self._invoke(lambda: session.app.FileSaveAs(path))
        else:
            if (
                not session.normalized_full_name
                and not self._text(self._read(session.project, "FullName", "")).strip()
            ):
                raise MspError(
                    ErrorCode.INVALID_REQUEST,
                    "Untitled projects require an explicit absolute .mpp save path",
                )
            save = getattr(session.project, "Save", None)
            if callable(save):
                saved = self._invoke(save)
            else:
                session.project.Activate()
                saved = self._invoke(session.app.FileSave)
        if saved is False:
            raise MspError(ErrorCode.INVALID_REQUEST, "Microsoft Project refused or cancelled the save")
        observed_path = self._normalized_full_name(session.project)
        if path:
            expected_path = os.path.normcase(os.path.abspath(path))
            if observed_path != expected_path:
                raise _RereadMismatch("Microsoft Project saved to a different path than requested")
        if self._dirty(session.project):
            raise _RereadMismatch("Microsoft Project still reports unsaved changes after save")
        session.normalized_full_name = observed_path
        session.native_guid = self._native_guid(session.project)

    def _activate_session(self, session: _LiveSession) -> None:
        self._invoke(session.project.Activate)

    @staticmethod
    def _require_com_success(result: Any, operation: str, *, mismatch: bool = False) -> None:
        """Treat an explicit COM Boolean failure as failure; allow void/None variants."""
        if result is not False:
            return
        message = f"Microsoft Project reported that {operation} did not complete"
        if mismatch:
            raise _RereadMismatch(message)
        raise MspError(ErrorCode.BACKEND_EXECUTION_FAILED, message)

    @staticmethod
    def _require_schedule_success(result: Any, operation: str) -> None:
        if result is False:
            raise BackendExecutionError(
                f"Microsoft Project reported that {operation} did not complete",
                dispatch_state=DispatchState.MAY_HAVE_DISPATCHED,
            )

    @staticmethod
    def _validated_save_target(path: str, *, current: str, require_new: bool = False) -> str:
        target = Path(path)
        if not target.is_absolute() or target.suffix.lower() != ".mpp":
            raise MspError(ErrorCode.INVALID_REQUEST, "Microsoft Project save path must be an absolute .mpp path")
        if not target.parent.is_dir():
            raise MspError(ErrorCode.INVALID_REQUEST, "Microsoft Project save path parent directory does not exist")
        normalized_target = os.path.normcase(os.path.abspath(str(target.resolve())))
        if target.exists() and (require_new or current != normalized_target):
            raise MspError(
                ErrorCode.INVALID_REQUEST,
                "Save As target already exists; overwrite is not implicit",
            )
        return str(target)

    @staticmethod
    def _validated_open_target(path: str) -> str:
        target = Path(path)
        if not target.is_absolute():
            raise MspError(ErrorCode.INVALID_REQUEST, "Microsoft Project paths must be absolute")
        if target.suffix.lower() != ".mpp":
            raise MspError(ErrorCode.INVALID_REQUEST, "Only .mpp project files can be opened")
        if not target.is_file():
            raise MspError(ErrorCode.INVALID_REQUEST, "The Microsoft Project file does not exist")
        return str(target.resolve())

    @staticmethod
    def _validated_template_target(path: str) -> str:
        target = Path(path)
        if not target.is_absolute():
            raise MspError(ErrorCode.INVALID_REQUEST, "Microsoft Project template paths must be absolute")
        if target.suffix.lower() not in {".mpt", ".mpp"}:
            raise MspError(ErrorCode.INVALID_REQUEST, "Project templates must be .mpt or .mpp files")
        if not target.is_file():
            raise MspError(ErrorCode.INVALID_REQUEST, "The Microsoft Project template does not exist")
        return str(target.resolve())

    @staticmethod
    def _touch_project(project: Any) -> None:
        getattr(project, "Name")

    @staticmethod
    def _read(value: Any, name: str, default: Any = None) -> Any:
        try:
            result = getattr(value, name)
        except (AttributeError, TypeError):
            return default
        return default if result is None else result

    @staticmethod
    def _set_if_present(value: Any, name: str, new_value: Any) -> None:
        try:
            setattr(value, name, new_value)
        except (AttributeError, TypeError):
            return

    @staticmethod
    def _text(value: Any) -> str:
        return "" if value is None else str(value)

    @staticmethod
    def _number(value: Any, default: int | float = 0) -> int | float:
        try:
            return float(value) if isinstance(value, (float, Decimal)) else int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _rate_number(value: Any) -> float:
        if isinstance(value, (int, float, Decimal)):
            return float(value)
        text = str(value).strip()
        match = re.search(r"[-+]?\d[\d\s.,']*", text)
        if match is None:
            return 0.0
        numeric = re.sub(r"[\s']", "", match.group(0))
        if "," in numeric and "." in numeric:
            decimal = "," if numeric.rfind(",") > numeric.rfind(".") else "."
            grouping = "." if decimal == "," else ","
            numeric = numeric.replace(grouping, "").replace(decimal, ".")
        elif "," in numeric:
            numeric = numeric.replace(",", ".")
        try:
            return float(numeric)
        except ValueError:
            return 0.0

    @staticmethod
    def _mapped_enum(value: Any, mapping: dict[int, str]) -> str:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            text = str(value)
            if text in mapping.values():
                return text
            if text.upper() in mapping.values():
                return text.upper()
            return text.lower()
        return mapping.get(numeric, str(numeric))

    @staticmethod
    def _date_value(value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            # Project schedule values are local and the public contract is
            # timezone-naive. pywin32 can return UTC-aware values, so restore
            # the Windows local wall-clock value before removing the offset.
            if value.tzinfo is not None:
                value = value.astimezone().replace(tzinfo=None)
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value)
        return None if text.strip().upper() in {"NA", "NV"} else text

    @staticmethod
    def _time_value(value: Any) -> str:
        if isinstance(value, datetime):
            return value.time().isoformat()
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _iter(collection: Any) -> list[Any]:
        if collection is None:
            return []
        return [item for item in collection if item is not None]

    def _calendar_collection(self, project: Any) -> Any:
        calendars = self._read(project, "BaseCalendars", None)
        return calendars if calendars is not None else self._read(project, "Calendars", ())

    def _calendar_ref(self, calendar: Any) -> dict[str, Any] | None:
        if calendar is None:
            return None
        guid = self._text(self._read(calendar, "GUID", self._read(calendar, "Guid", "")))
        if not guid:
            return None
        return ObjectRef(kind=ObjectKind.CALENDAR, guid=guid).model_dump(mode="json")

    def _resource_base_calendar_ref(self, project: Any, resource: Any) -> dict[str, Any] | None:
        name = self._text(self._read(resource, "BaseCalendar", "")).strip()
        if not name:
            resource_calendar = self._read(resource, "Calendar", None)
            base = self._read(resource_calendar, "BaseCalendar", None)
            name = self._text(self._read(base, "Name", base)).strip()
        if not name:
            return None
        for calendar in self._iter(self._calendar_collection(project)):
            if self._text(self._read(calendar, "Name", "")) == name:
                return self._calendar_ref(calendar)
        return None

    def _normalized_full_name(self, project: Any) -> str:
        value = self._text(self._read(project, "FullName", "")).strip()
        return os.path.normcase(os.path.abspath(value)) if value else ""

    def _native_guid(self, project: Any) -> str | None:
        for name in ("ProjectGUID", "GUID", "Guid"):
            value = self._read(project, name, None)
            if value:
                return str(value)
        return None

    def _dirty(self, project: Any) -> bool:
        return not bool(self._read(project, "Saved", False))

    def _state_on_sta(self, session: _LiveSession) -> ProjectState:
        project = session.project
        snapshot = {
            "project": self._project_items(project),
            "tasks": self._tasks(project),
            "task_order": [
                int(task.UniqueID) for task in self._iter(self._read(project, "Tasks", ()))
            ],
            "dependencies": self._dependencies(project),
            "resources": self._resources(project),
            "assignments": self._assignments(project),
            "calendars": self._calendars(project),
            "baselines": self._baselines(project),
            "status": self._status_items(project),
        }
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return ProjectState(token=f"sha256:{hashlib.sha256(encoded).hexdigest()}")

    def _projection(self, project: Any, entity: QueryEntity) -> list[dict[str, Any]]:
        readers = {
            QueryEntity.PROJECT: self._project_items,
            QueryEntity.TASK: self._tasks,
            QueryEntity.DEPENDENCY: self._dependencies,
            QueryEntity.RESOURCE: self._resources,
            QueryEntity.ASSIGNMENT: self._assignments,
            QueryEntity.CALENDAR: self._calendars,
            QueryEntity.BASELINE: self._baselines,
            QueryEntity.STATUS: self._status_items,
        }
        return readers[entity](project)

    def _project_items(self, project: Any) -> list[dict[str, Any]]:
        summary = self._read(project, "ProjectSummaryTask", None)
        return [
            {
                "name": self._text(self._read(project, "Name", "")),
                "full_name": self._text(self._read(project, "FullName", "")) or None,
                "saved": bool(self._read(project, "Saved", False)),
                "project_start": self._date_value(self._read(project, "ProjectStart", None)),
                "project_finish": self._date_value(self._read(project, "ProjectFinish", None)),
                "schedule_from": "start" if bool(self._read(project, "ScheduleFromStart", True)) else "finish",
                "current_date": self._date_value(self._read(project, "CurrentDate", None)),
                "calendar_ref": self._calendar_ref(self._read(project, "Calendar", None)),
                "priority": int(self._number(self._read(summary, "Priority", 500), 500)) if summary is not None else 500,
                "default_task_type": self._mapped_enum(
                    self._read(project, "DefaultTaskType", 0), _TASK_TYPE_FROM_NATIVE
                ),
                "default_effort_driven": bool(self._read(project, "DefaultEffortDriven", False)),
                "new_tasks_manual": bool(self._read(project, "NewTasksCreatedAsManual", False)),
                "honor_constraints": bool(self._read(project, "HonorConstraints", True)),
                "multiple_critical_paths": bool(self._read(project, "MultipleCriticalPaths", False)),
                "hours_per_day": self._number(self._read(project, "HoursPerDay", 8)),
                "hours_per_week": self._number(self._read(project, "HoursPerWeek", 40)),
                "days_per_month": self._number(self._read(project, "DaysPerMonth", 20)),
                "title": self._text(self._read(project, "Title", "")),
                "manager": self._text(self._read(project, "Manager", "")),
                "company": self._text(self._read(project, "Company", "")),
                "subject": self._text(self._read(project, "Subject", "")),
                "author": self._text(self._read(project, "Author", "")),
                "keywords": self._text(self._read(project, "Keywords", "")),
                "comments": self._text(self._read(project, "ProjectNotes", self._read(project, "Comments", ""))),
            }
        ]

    def _tasks(self, project: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for task in self._iter(self._read(project, "Tasks", ())):
            unique_id = int(self._read(task, "UniqueID"))
            parent = self._read(task, "OutlineParent", None)
            parent_uid = self._read(parent, "UniqueID", None) if parent is not None else None
            items.append(
                {
                    "ref": ObjectRef(kind=ObjectKind.TASK, unique_id=unique_id).model_dump(mode="json"),
                    "name": self._text(self._read(task, "Name", "")),
                    "duration_minutes": int(self._number(self._read(task, "Duration", 0))),
                    "milestone": bool(self._read(task, "Milestone", False)),
                    "start": self._date_value(self._read(task, "Start", None)),
                    "finish": self._date_value(self._read(task, "Finish", None)),
                    "critical": bool(self._read(task, "Critical", False)),
                    "total_slack_minutes": int(self._number(self._read(task, "TotalSlack", 0))),
                    "constraint_type": self._number(self._read(task, "ConstraintType", 0)),
                    "constraint_type_name": self._mapped_enum(
                        self._read(task, "ConstraintType", 0), _TASK_CONSTRAINT_FROM_NATIVE
                    ),
                    "constraint_date": self._date_value(self._read(task, "ConstraintDate", None)),
                    "deadline": self._date_value(self._read(task, "Deadline", None)),
                    "task_type": self._mapped_enum(self._read(task, "Type", 0), _TASK_TYPE_FROM_NATIVE),
                    "effort_driven": bool(self._read(task, "EffortDriven", False)),
                    "manual": bool(self._read(task, "Manual", False)),
                    "priority": int(self._number(self._read(task, "Priority", 500), 500)),
                    "notes": self._text(self._read(task, "Notes", "")),
                    "calendar_ref": self._calendar_ref(self._read(task, "CalendarObject", None)),
                    "ignore_resource_calendar": bool(
                        self._read(task, "IgnoreResourceCalendar", False)
                    ),
                    "outline_level": int(self._number(self._read(task, "OutlineLevel", 1), 1)),
                    "outline_number": self._text(self._read(task, "OutlineNumber", "")),
                    "parent_ref": (
                        ObjectRef(kind=ObjectKind.TASK, unique_id=int(parent_uid)).model_dump(mode="json")
                        if parent_uid
                        else None
                    ),
                    "percent_complete": self._number(self._read(task, "PercentComplete", 0)),
                    "actual_duration_minutes": int(self._number(self._read(task, "ActualDuration", 0))),
                    "remaining_duration_minutes": int(self._number(self._read(task, "RemainingDuration", 0))),
                    "actual_work_minutes": int(self._number(self._read(task, "ActualWork", 0))),
                    "remaining_work_minutes": int(self._number(self._read(task, "RemainingWork", 0))),
                    "actual_start": self._date_value(self._read(task, "ActualStart", None)),
                    "actual_finish": self._date_value(self._read(task, "ActualFinish", None)),
                    "fixed_cost": self._number(self._read(task, "FixedCost", 0)),
                    "cost_accrual": self._mapped_enum(
                        self._read(task, "FixedCostAccrual", 3), _COST_ACCRUAL_FROM_NATIVE
                    ),
                    "cost": self._number(self._read(task, "Cost", 0)),
                    "baseline_cost": self._number(self._read(task, "BaselineCost", 0)),
                    "cost_variance": self._number(self._read(task, "CostVariance", 0)),
                    "finish_variance_minutes": int(self._number(self._read(task, "FinishVariance", 0))),
                    "bcws": self._number(self._read(task, "BCWS", 0)),
                    "bcwp": self._number(self._read(task, "BCWP", 0)),
                    "acwp": self._number(self._read(task, "ACWP", 0)),
                    "schedule_variance": self._number(self._read(task, "SV", 0)),
                }
            )
        return sorted(items, key=lambda item: item["ref"]["unique_id"])

    def _dependencies(self, project: Any) -> list[dict[str, Any]]:
        seen: set[tuple[int, int, str, int]] = set()
        items: list[dict[str, Any]] = []
        direct = self._read(project, "Dependencies", None)
        dependencies = self._iter(direct)
        if direct is None:
            for task in self._iter(self._read(project, "Tasks", ())):
                dependencies.extend(self._iter(self._read(task, "TaskDependencies", ())))
        for dependency in dependencies:
            predecessor = self._read(dependency, "From", self._read(dependency, "Predecessor", None))
            successor = self._read(dependency, "To", self._read(dependency, "Successor", None))
            pred_uid = int(self._read(predecessor, "UniqueID"))
            succ_uid = int(self._read(successor, "UniqueID"))
            dep_type = self._mapped_enum(self._read(dependency, "Type", 1), _TASK_LINK_FROM_NATIVE)
            lag = int(self._number(self._read(dependency, "Lag", 0)))
            key = (pred_uid, succ_uid, dep_type, lag)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "predecessor": ObjectRef(kind=ObjectKind.TASK, unique_id=pred_uid).model_dump(mode="json"),
                    "successor": ObjectRef(kind=ObjectKind.TASK, unique_id=succ_uid).model_dump(mode="json"),
                    "dependency_type": dep_type,
                    "lag_minutes": lag,
                }
            )
        return sorted(items, key=lambda item: (item["predecessor"]["unique_id"], item["successor"]["unique_id"]))

    def _resources(self, project: Any) -> list[dict[str, Any]]:
        items = []
        for resource in self._iter(self._read(project, "Resources", ())):
            uid = int(self._read(resource, "UniqueID"))
            resource_type = self._mapped_enum(self._read(resource, "Type", 0), _RESOURCE_TYPE_FROM_NATIVE)
            work = resource_type == ResourceType.WORK.value
            rate_based = resource_type in {ResourceType.WORK.value, ResourceType.MATERIAL.value}
            material = resource_type == ResourceType.MATERIAL.value
            items.append(
                {
                    "ref": ObjectRef(kind=ObjectKind.RESOURCE, unique_id=uid).model_dump(mode="json"),
                    "name": self._text(self._read(resource, "Name", "")),
                    "resource_type": resource_type,
                    "max_units_percent": self._number(self._read(resource, "MaxUnits", 0)) * 100 if work else None,
                    "standard_rate": self._rate_number(self._read(resource, "StandardRate", 0)) if rate_based else None,
                    "standard_rate_basis": "hour" if work else "material_unit" if material else None,
                    "overtime_rate_per_hour": self._rate_number(self._read(resource, "OvertimeRate", 0)) if work else None,
                    "cost_per_use": self._rate_number(self._read(resource, "CostPerUse", 0)) if rate_based else None,
                    "material_label": self._text(self._read(resource, "MaterialLabel", "")) or None if material else None,
                    "cost_accrual": self._mapped_enum(
                        self._read(resource, "AccrueAt", 3), _COST_ACCRUAL_FROM_NATIVE
                    ),
                    "initials": self._text(self._read(resource, "Initials", "")),
                    "group": self._text(self._read(resource, "Group", "")),
                    "code": self._text(self._read(resource, "Code", "")),
                    "email": self._text(self._read(resource, "EMailAddress", "")),
                    "notes": self._text(self._read(resource, "Notes", "")),
                    "base_calendar_ref": self._resource_base_calendar_ref(project, resource)
                    if work else None,
                    "overallocated": bool(self._read(resource, "Overallocated", False)),
                }
            )
        return sorted(items, key=lambda item: item["ref"]["unique_id"])

    def _assignments(self, project: Any) -> list[dict[str, Any]]:
        items = []
        for assignment in self._assignment_objects(project):
            uid = int(self._read(assignment, "UniqueID"))
            task = self._read(assignment, "Task")
            resource = self._read(assignment, "Resource")
            resource_type = self._mapped_enum(self._read(resource, "Type", 0), _RESOURCE_TYPE_FROM_NATIVE)
            work = resource_type == ResourceType.WORK.value
            material = resource_type == ResourceType.MATERIAL.value
            cost_resource = resource_type == ResourceType.COST.value
            items.append(
                {
                    "ref": ObjectRef(kind=ObjectKind.ASSIGNMENT, unique_id=uid).model_dump(mode="json"),
                    "task_ref": ObjectRef(
                        kind=ObjectKind.TASK,
                        unique_id=int(self._read(task, "UniqueID")),
                    ).model_dump(mode="json"),
                    "resource_ref": ObjectRef(
                        kind=ObjectKind.RESOURCE,
                        unique_id=int(self._read(resource, "UniqueID")),
                    ).model_dump(mode="json"),
                    "units_percent": self._number(self._read(assignment, "Units", 0)) * 100 if work else None,
                    "material_units": self._number(self._read(assignment, "Units", 0)) if material else None,
                    "work_minutes": int(self._number(self._read(assignment, "Work", 0))) if work else None,
                    "actual_work_minutes": int(self._number(self._read(assignment, "ActualWork", 0))),
                    "cost_rate_table": self._mapped_enum(
                        self._read(assignment, "CostRateTable", 0), _COST_RATE_FROM_NATIVE
                    ) if not cost_resource else None,
                    "cost": self._rate_number(self._read(assignment, "Cost", 0)) if cost_resource else None,
                }
            )
        return sorted(items, key=lambda item: item["ref"]["unique_id"])

    def _assignment_objects(self, project: Any) -> list[Any]:
        assignments: list[Any] = []
        seen: set[int] = set()
        for task in self._iter(self._read(project, "Tasks", ())):
            for assignment in self._iter(self._read(task, "Assignments", ())):
                uid = int(self._read(assignment, "UniqueID", 0))
                if uid and uid not in seen:
                    seen.add(uid)
                    assignments.append(assignment)
        return assignments

    def _calendars(self, project: Any) -> list[dict[str, Any]]:
        items = []
        for calendar in self._iter(self._calendar_collection(project)):
            guid = self._text(
                self._read(calendar, "GUID", self._read(calendar, "Guid", self._read(calendar, "ProjectGUID", "")))
            )
            if not guid:
                raise MspError(ErrorCode.UNSUPPORTED_OPERATION, "Calendar does not expose a stable GUID")
            base = self._read(calendar, "BaseCalendar", None)
            base_guid = None
            if base is not None:
                base_guid = self._text(self._read(base, "GUID", self._read(base, "Guid", ""))) or None
            weekly = []
            for day in self._iter(self._read(calendar, "WeekDays", ())):
                intervals = []
                for index in range(1, 6):
                    shift = self._read(day, f"Shift{index}", None)
                    start = self._read(shift, "Start", None) if shift is not None else None
                    finish = self._read(shift, "Finish", None) if shift is not None else None
                    if start not in (None, "") and finish not in (None, ""):
                        intervals.append({"start": self._time_value(start), "end": self._time_value(finish)})
                weekly.append(
                    {
                        "weekday": _WEEKDAY_FROM_NATIVE.get(int(self._read(day, "Index", 0)), str(self._read(day, "Index", 0))),
                        "working": bool(self._read(day, "Working", bool(intervals))),
                        "intervals": intervals,
                    }
                )
            exceptions = []
            for exception in self._iter(self._read(calendar, "Exceptions", ())):
                intervals = []
                for index in range(1, 6):
                    shift = self._read(exception, f"Shift{index}", None)
                    start = self._read(shift, "Start", None) if shift is not None else None
                    finish = self._read(shift, "Finish", None) if shift is not None else None
                    if start not in (None, "") and finish not in (None, ""):
                        intervals.append({"start": self._time_value(start), "end": self._time_value(finish)})
                exceptions.append(
                    {
                        "name": self._text(self._read(exception, "Name", "")),
                        "start_date": self._date_value(self._read(exception, "Start", None)),
                        "end_date": self._date_value(self._read(exception, "Finish", None)),
                        "working": bool(intervals),
                        "intervals": intervals,
                    }
                )
            items.append(
                {
                    "ref": ObjectRef(kind=ObjectKind.CALENDAR, guid=guid).model_dump(mode="json"),
                    "name": self._text(self._read(calendar, "Name", "")),
                    "base_calendar_ref": (
                        ObjectRef(kind=ObjectKind.CALENDAR, guid=base_guid).model_dump(mode="json")
                        if base_guid
                        else None
                    ),
                    "weekly": weekly,
                    "exceptions": exceptions,
                }
            )
        return sorted(items, key=lambda item: item["ref"]["guid"])

    def _baselines(self, project: Any) -> list[dict[str, Any]]:
        explicit = self._read(project, "Baselines", None)
        if explicit is not None:
            active = {int(value) for value in explicit}
        else:
            active = set()
            tasks = self._iter(self._read(project, "Tasks", ()))
            for index in range(11):
                field = "BaselineStart" if index == 0 else f"Baseline{index}Start"
                if any(self._baseline_value_is_set(self._read(task, field, None)) for task in tasks):
                    active.add(index)
        return [{"baseline": index, "set": index in active} for index in range(11)]

    @staticmethod
    def _baseline_value_is_set(value: Any) -> bool:
        if value in (None, ""):
            return False
        if isinstance(value, str):
            return value.strip().upper() not in {"NA", "N/A"}
        if isinstance(value, (int, float, Decimal)) and float(value) >= 4_000_000_000:
            return False
        return True

    def _status_items(self, project: Any) -> list[dict[str, Any]]:
        return [{"status_date": self._date_value(self._read(project, "StatusDate", None))}]

    @staticmethod
    def _allowed_fields(entity: QueryEntity) -> set[str]:
        fields = {
            QueryEntity.PROJECT: {
                "name", "full_name", "saved", "project_start", "project_finish", "schedule_from",
                "current_date", "calendar_ref", "priority", "default_task_type",
                "default_effort_driven", "new_tasks_manual", "honor_constraints",
                "multiple_critical_paths", "hours_per_day", "hours_per_week", "days_per_month",
                "title", "manager", "company", "subject", "author", "keywords", "comments"
            },
            QueryEntity.TASK: {
                "ref", "name", "duration_minutes", "milestone", "parent_ref", "percent_complete",
                "actual_duration_minutes", "remaining_duration_minutes", "actual_work_minutes",
                "remaining_work_minutes", "actual_start", "actual_finish", "fixed_cost", "cost_accrual",
                "start", "finish", "critical", "total_slack_minutes", "constraint_type",
                "constraint_type_name", "constraint_date", "deadline", "task_type", "effort_driven",
                "manual", "priority", "notes", "calendar_ref", "ignore_resource_calendar",
                "outline_level", "outline_number",
                "cost", "baseline_cost", "cost_variance", "finish_variance_minutes", "bcws", "bcwp",
                "acwp", "schedule_variance"
            },
            QueryEntity.DEPENDENCY: {"predecessor", "successor", "dependency_type", "lag_minutes"},
            QueryEntity.RESOURCE: {
                "ref", "name", "resource_type", "max_units_percent", "standard_rate", "standard_rate_basis",
                "overtime_rate_per_hour", "cost_per_use", "material_label", "cost_accrual",
                "initials", "group", "code", "email", "notes", "base_calendar_ref", "overallocated"
            },
            QueryEntity.ASSIGNMENT: {
                "ref", "task_ref", "resource_ref", "units_percent", "material_units", "work_minutes",
                "actual_work_minutes", "cost_rate_table", "cost"
            },
            QueryEntity.CALENDAR: {"ref", "name", "base_calendar_ref", "weekly", "exceptions"},
            QueryEntity.BASELINE: {"baseline", "set"},
            QueryEntity.STATUS: {"status_date"},
        }
        return fields[entity]

    @staticmethod
    def _entity_for_kind(kind: ObjectKind) -> QueryEntity:
        mapping = {
            ObjectKind.TASK: QueryEntity.TASK,
            ObjectKind.RESOURCE: QueryEntity.RESOURCE,
            ObjectKind.ASSIGNMENT: QueryEntity.ASSIGNMENT,
            ObjectKind.CALENDAR: QueryEntity.CALENDAR,
        }
        try:
            return mapping[kind]
        except KeyError as exc:
            raise MspError(ErrorCode.INVALID_REQUEST, "Unsupported reference kind") from exc
    CreateAssignment,
    CreateCalendar,
    CreateResource,
    CreateTask,
    DeleteAssignment,
    DeleteCalendar,
    DeleteResource,
    DeleteTask,
    MoveTask,
    RemoveDependency,
    SetBaseline,
    SetStatusDate,
    TaskProgressUpdate,
    TimephasedWorkUpdate,
    UpdateAssignment,
    UpdateCalendar,
    UpdateProjectProperties,
    UpdateResource,
    UpdateTask,
