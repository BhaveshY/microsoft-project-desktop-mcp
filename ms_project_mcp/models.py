from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .compat import StrEnum


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class Ownership(StrEnum):
    SERVER_OWNED = "server_owned"
    ATTACHED_USER_OWNED = "attached_user_owned"


class DesktopSmoke(StrEnum):
    NOT_VERIFIED = "not_verified"
    PASSED = "passed"
    FAILED = "failed"


class ContractFidelity(StrEnum):
    LIVE_NATIVE = "live_native"
    CONTRACT_ONLY = "contract_only"


class VerificationLevel(StrEnum):
    NATIVE_REREAD = "native_reread"
    STRUCTURAL = "structural"


class Atomicity(StrEnum):
    UNDO_ATOMIC = "undo_atomic"
    CHECKPOINTED = "checkpointed"
    NON_ATOMIC = "non_atomic"


class CommitState(StrEnum):
    PLANNED = "planned"
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    UNKNOWN_COMMIT_STATE = "unknown_commit_state"


class BatchMode(StrEnum):
    PLAN = "plan"
    COMMIT = "commit"


class ObjectKind(StrEnum):
    TASK = "task"
    RESOURCE = "resource"
    ASSIGNMENT = "assignment"
    CALENDAR = "calendar"
    DEPENDENCY = "dependency"


class ProjectAction(StrEnum):
    CREATE = "create"
    OPEN = "open"
    ATTACH = "attach"
    SAVE = "save"
    DETACH = "detach"
    CLOSE = "close"


class CloseDisposition(StrEnum):
    SAVE_AND_CLOSE = "save_and_close"
    DISCARD_AND_CLOSE = "discard_and_close"
    REFUSE_IF_DIRTY = "refuse_if_dirty"


class QueryEntity(StrEnum):
    PROJECT = "project"
    TASK = "task"
    DEPENDENCY = "dependency"
    RESOURCE = "resource"
    ASSIGNMENT = "assignment"
    CALENDAR = "calendar"
    BASELINE = "baseline"
    STATUS = "status"


class ScheduleCommand(StrEnum):
    CALCULATE = "calculate"
    LEVEL = "level"
    CLEAR_LEVELING = "clear_leveling"
    RESCHEDULE = "reschedule"


class AnalysisKind(StrEnum):
    SCHEDULE_HEALTH = "schedule_health"
    CRITICAL_PATH = "critical_path"
    CONSTRAINTS = "constraints"
    SLACK = "slack"
    OVERALLOCATIONS = "overallocations"
    VARIANCE = "variance"
    EARNED_VALUE = "earned_value"
    CHANGE_IMPACT = "change_impact"


class ResourceType(StrEnum):
    WORK = "work"
    MATERIAL = "material"
    COST = "cost"


class CostAccrual(StrEnum):
    START = "start"
    END = "end"
    PRORATED = "prorated"


class Weekday(StrEnum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


class ProjectRef(ContractModel):
    session_id: str = Field(min_length=1, max_length=128)
    project_key: str = Field(min_length=1, max_length=512)


class ProjectState(ContractModel):
    token: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ObjectRef(ContractModel):
    kind: ObjectKind
    guid: str | None = None
    unique_id: int | None = Field(default=None, ge=1)
    client_ref: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def exactly_one_identity(self) -> ObjectRef:
        values = (self.guid, self.unique_id, self.client_ref)
        if sum(value is not None for value in values) != 1:
            raise ValueError("ObjectRef requires exactly one of guid, unique_id, or client_ref")
        return self


def _require_kind(ref: ObjectRef | None, kind: ObjectKind, field_name: str) -> None:
    if ref is not None and ref.kind != kind:
        raise ValueError(f"{field_name} requires a {kind.value} reference")


def _naive(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        raise ValueError(f"{field_name} must be timezone-naive Microsoft Project local time")
    return value


class CreateTask(ContractModel):
    op: Literal["create_task"] = "create_task"
    client_ref: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    duration_minutes: int = Field(default=480, ge=0)
    milestone: bool = False
    parent: ObjectRef | None = None
    after: ObjectRef | None = None
    fixed_cost: Decimal = Field(default=Decimal("0"), ge=0)
    cost_accrual: CostAccrual = CostAccrual.PRORATED

    @model_validator(mode="after")
    def valid_task(self) -> CreateTask:
        if self.milestone and self.duration_minutes != 0:
            raise ValueError("milestone tasks require duration_minutes=0")
        _require_kind(self.parent, ObjectKind.TASK, "parent")
        _require_kind(self.after, ObjectKind.TASK, "after")
        return self


class UpdateTask(ContractModel):
    op: Literal["update_task"] = "update_task"
    task: ObjectRef
    name: str | None = Field(default=None, min_length=1, max_length=255)
    duration_minutes: int | None = Field(default=None, ge=0)
    fixed_cost: Decimal | None = Field(default=None, ge=0)
    cost_accrual: CostAccrual | None = None

    @model_validator(mode="after")
    def valid_update(self) -> UpdateTask:
        _require_kind(self.task, ObjectKind.TASK, "task")
        if all(value is None for value in (self.name, self.duration_minutes, self.fixed_cost, self.cost_accrual)):
            raise ValueError("update_task requires at least one changed planning field")
        return self


class MoveTask(ContractModel):
    op: Literal["move_task"] = "move_task"
    task: ObjectRef
    parent: ObjectRef | None = None
    after: ObjectRef | None = None
    to_root: bool = False

    @model_validator(mode="after")
    def valid_move(self) -> MoveTask:
        for name, ref in (("task", self.task), ("parent", self.parent), ("after", self.after)):
            _require_kind(ref, ObjectKind.TASK, name)
        if self.parent is None and self.after is None and not self.to_root:
            raise ValueError("move_task requires parent, after, or to_root=true")
        if self.to_root and self.parent is not None:
            raise ValueError("to_root cannot be combined with parent")
        return self


class DeleteTask(ContractModel):
    op: Literal["delete_task"] = "delete_task"
    task: ObjectRef
    recursive: bool = False

    @model_validator(mode="after")
    def task_kind_only(self) -> DeleteTask:
        _require_kind(self.task, ObjectKind.TASK, "task")
        return self


class CreateResource(ContractModel):
    op: Literal["create_resource"] = "create_resource"
    client_ref: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    resource_type: ResourceType = ResourceType.WORK
    max_units_percent: Decimal = Field(default=Decimal("100"), ge=0)
    standard_rate: Decimal = Field(default=Decimal("0"), ge=0)
    overtime_rate_per_hour: Decimal = Field(default=Decimal("0"), ge=0)
    cost_per_use: Decimal = Field(default=Decimal("0"), ge=0)
    material_label: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def valid_type_fields(self) -> CreateResource:
        if self.resource_type == ResourceType.WORK and self.material_label is not None:
            raise ValueError("material_label is only valid for material resources")
        if self.resource_type == ResourceType.MATERIAL:
            if self.max_units_percent != Decimal("100"):
                raise ValueError("max_units_percent is not valid for material resources")
            if self.overtime_rate_per_hour != Decimal("0"):
                raise ValueError("overtime_rate_per_hour is not valid for material resources")
        if self.resource_type == ResourceType.COST:
            if (
                self.max_units_percent != Decimal("100")
                or self.standard_rate != Decimal("0")
                or self.overtime_rate_per_hour != Decimal("0")
                or self.cost_per_use != Decimal("0")
                or self.material_label is not None
            ):
                raise ValueError("cost resources do not accept units, rate, per-use, or material fields")
        return self


class UpdateResource(ContractModel):
    op: Literal["update_resource"] = "update_resource"
    resource: ObjectRef
    name: str | None = Field(default=None, min_length=1, max_length=255)
    max_units_percent: Decimal | None = Field(default=None, ge=0)
    standard_rate: Decimal | None = Field(default=None, ge=0)
    overtime_rate_per_hour: Decimal | None = Field(default=None, ge=0)
    cost_per_use: Decimal | None = Field(default=None, ge=0)
    material_label: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def valid_update(self) -> UpdateResource:
        _require_kind(self.resource, ObjectKind.RESOURCE, "resource")
        values = (
            self.name,
            self.max_units_percent,
            self.standard_rate,
            self.overtime_rate_per_hour,
            self.cost_per_use,
            self.material_label,
        )
        if all(value is None for value in values):
            raise ValueError("update_resource requires at least one changed field")
        return self


class DeleteResource(ContractModel):
    op: Literal["delete_resource"] = "delete_resource"
    resource: ObjectRef

    @model_validator(mode="after")
    def resource_kind_only(self) -> DeleteResource:
        _require_kind(self.resource, ObjectKind.RESOURCE, "resource")
        return self


class CreateAssignment(ContractModel):
    op: Literal["create_assignment"] = "create_assignment"
    client_ref: str = Field(min_length=1, max_length=128)
    task: ObjectRef
    resource: ObjectRef
    units_percent: Decimal = Field(default=Decimal("100"), ge=0)
    material_units: Decimal | None = Field(default=None, ge=0)
    work_minutes: int | None = Field(default=None, ge=0)
    cost_rate_table: Literal["A", "B", "C", "D", "E"] = "A"
    cost: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_refs(self) -> CreateAssignment:
        _require_kind(self.task, ObjectKind.TASK, "task")
        _require_kind(self.resource, ObjectKind.RESOURCE, "resource")
        return self


class UpdateAssignment(ContractModel):
    op: Literal["update_assignment"] = "update_assignment"
    assignment: ObjectRef
    units_percent: Decimal | None = Field(default=None, ge=0)
    material_units: Decimal | None = Field(default=None, ge=0)
    work_minutes: int | None = Field(default=None, ge=0)
    cost_rate_table: Literal["A", "B", "C", "D", "E"] | None = None
    cost: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_update(self) -> UpdateAssignment:
        _require_kind(self.assignment, ObjectKind.ASSIGNMENT, "assignment")
        if all(
            value is None
            for value in (
                self.units_percent,
                self.material_units,
                self.work_minutes,
                self.cost_rate_table,
                self.cost,
            )
        ):
            raise ValueError("update_assignment requires at least one changed field")
        return self


class DeleteAssignment(ContractModel):
    op: Literal["delete_assignment"] = "delete_assignment"
    assignment: ObjectRef

    @model_validator(mode="after")
    def assignment_kind_only(self) -> DeleteAssignment:
        _require_kind(self.assignment, ObjectKind.ASSIGNMENT, "assignment")
        return self


class AddDependency(ContractModel):
    op: Literal["add_dependency"] = "add_dependency"
    predecessor: ObjectRef
    successor: ObjectRef
    dependency_type: Literal["FS", "SS", "FF", "SF"] = "FS"
    lag_minutes: int = 0

    @model_validator(mode="after")
    def task_refs_only(self) -> AddDependency:
        _require_kind(self.predecessor, ObjectKind.TASK, "predecessor")
        _require_kind(self.successor, ObjectKind.TASK, "successor")
        return self


class RemoveDependency(ContractModel):
    op: Literal["remove_dependency"] = "remove_dependency"
    predecessor: ObjectRef
    successor: ObjectRef

    @model_validator(mode="after")
    def task_refs_only(self) -> RemoveDependency:
        _require_kind(self.predecessor, ObjectKind.TASK, "predecessor")
        _require_kind(self.successor, ObjectKind.TASK, "successor")
        return self


class WorkingInterval(ContractModel):
    start: time
    end: time

    @model_validator(mode="after")
    def increasing(self) -> WorkingInterval:
        if self.start >= self.end:
            raise ValueError("working interval start must precede end")
        return self


class WorkingDay(ContractModel):
    weekday: Weekday
    intervals: tuple[WorkingInterval, ...] = Field(max_length=5)

    @model_validator(mode="after")
    def non_overlapping(self) -> WorkingDay:
        ordered = sorted(self.intervals, key=lambda interval: interval.start)
        if any(left.end > right.start for left, right in zip(ordered, ordered[1:])):
            raise ValueError("working intervals may not overlap")
        return self


class CalendarException(ContractModel):
    name: str = Field(min_length=1, max_length=255)
    start_date: date
    end_date: date
    working: bool = False
    intervals: tuple[WorkingInterval, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def valid_exception(self) -> CalendarException:
        if self.end_date < self.start_date:
            raise ValueError("calendar exception end_date must not precede start_date")
        if not self.working and self.intervals:
            raise ValueError("non-working exceptions cannot contain working intervals")
        return self


class CreateCalendar(ContractModel):
    op: Literal["create_calendar"] = "create_calendar"
    client_ref: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    base_calendar: ObjectRef | None = None
    weekly: tuple[WorkingDay, ...] = ()
    exceptions: tuple[CalendarException, ...] = ()

    @model_validator(mode="after")
    def valid_calendar(self) -> CreateCalendar:
        _require_kind(self.base_calendar, ObjectKind.CALENDAR, "base_calendar")
        if len({day.weekday for day in self.weekly}) != len(self.weekly):
            raise ValueError("calendar weekly definitions require unique weekdays")
        return self


class UpdateCalendar(ContractModel):
    op: Literal["update_calendar"] = "update_calendar"
    calendar: ObjectRef
    name: str | None = Field(default=None, min_length=1, max_length=255)
    weekly: tuple[WorkingDay, ...] | None = None
    exceptions: tuple[CalendarException, ...] | None = None

    @model_validator(mode="after")
    def valid_update(self) -> UpdateCalendar:
        _require_kind(self.calendar, ObjectKind.CALENDAR, "calendar")
        if self.name is None and self.weekly is None and self.exceptions is None:
            raise ValueError("update_calendar requires at least one changed field")
        if self.weekly is not None and len({day.weekday for day in self.weekly}) != len(self.weekly):
            raise ValueError("calendar weekly definitions require unique weekdays")
        return self


class DeleteCalendar(ContractModel):
    op: Literal["delete_calendar"] = "delete_calendar"
    calendar: ObjectRef

    @model_validator(mode="after")
    def calendar_kind_only(self) -> DeleteCalendar:
        _require_kind(self.calendar, ObjectKind.CALENDAR, "calendar")
        return self


class UpdateProjectProperties(ContractModel):
    op: Literal["update_project_properties"] = "update_project_properties"
    title: str | None = Field(default=None, max_length=255)
    manager: str | None = Field(default=None, max_length=255)
    company: str | None = Field(default=None, max_length=255)
    subject: str | None = Field(default=None, max_length=255)
    comments: str | None = Field(default=None, max_length=4000)
    project_start: datetime | None = None

    @field_validator("project_start")
    @classmethod
    def project_local_time(cls, value: datetime | None) -> datetime | None:
        return _naive(value, "project_start")

    @model_validator(mode="after")
    def has_change(self) -> UpdateProjectProperties:
        values = (self.title, self.manager, self.company, self.subject, self.comments, self.project_start)
        if all(value is None for value in values):
            raise ValueError("update_project_properties requires at least one changed field")
        return self


class SetBaseline(ContractModel):
    op: Literal["set_baseline"] = "set_baseline"
    baseline: int = Field(default=0, ge=0, le=10)


class ClearBaseline(ContractModel):
    op: Literal["clear_baseline"] = "clear_baseline"
    baseline: int = Field(default=0, ge=0, le=10)


Operation = Annotated[
    CreateTask
    | UpdateTask
    | MoveTask
    | DeleteTask
    | CreateResource
    | UpdateResource
    | DeleteResource
    | CreateAssignment
    | UpdateAssignment
    | DeleteAssignment
    | AddDependency
    | RemoveDependency
    | CreateCalendar
    | UpdateCalendar
    | DeleteCalendar
    | UpdateProjectProperties
    | SetBaseline
    | ClearBaseline,
    Field(discriminator="op"),
]


class OperationBatch(ContractModel):
    operations: tuple[Operation, ...] = Field(min_length=1, max_length=500)
    expected_state: ProjectState
    idempotency_key: str = Field(min_length=8, max_length=128)
    mode: BatchMode
    verification: VerificationLevel = VerificationLevel.NATIVE_REREAD
    confirmation_token: str | None = None


class ProjectRequest(ContractModel):
    action: ProjectAction
    name: str | None = Field(default=None, min_length=1, max_length=255)
    path: str | None = None
    project: ProjectRef | None = None
    expected_state: ProjectState | None = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    close_disposition: CloseDisposition = CloseDisposition.REFUSE_IF_DIRTY
    confirmation_token: str | None = None

    @model_validator(mode="after")
    def lifecycle_identity(self) -> ProjectRequest:
        if self.action in {ProjectAction.CREATE, ProjectAction.OPEN, ProjectAction.ATTACH}:
            if self.idempotency_key is None:
                raise ValueError("create, open, and attach require idempotency_key")
        return self


class QueryRequest(ContractModel):
    project: ProjectRef
    entity: QueryEntity
    fields: tuple[str, ...] = ()
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = None


class ApplyRequest(ContractModel):
    project: ProjectRef
    batch: OperationBatch


class ScheduleOptions(ContractModel):
    status_date: datetime | None = None
    reschedule_uncompleted_work_to: datetime | None = None
    clear_existing_leveling: bool = False

    @field_validator("status_date", "reschedule_uncompleted_work_to")
    @classmethod
    def project_local_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return _naive(value, info.field_name)


class ScheduleRequest(ContractModel):
    project: ProjectRef
    command: ScheduleCommand
    expected_state: ProjectState
    idempotency_key: str = Field(min_length=8, max_length=128)
    mode: BatchMode
    confirmation_token: str | None = None
    options: ScheduleOptions = Field(default_factory=ScheduleOptions)


class TaskProgressUpdate(ContractModel):
    op: Literal["task_progress"] = "task_progress"
    task: ObjectRef
    percent_complete: Decimal | None = Field(default=None, ge=0, le=100)
    actual_duration_minutes: int | None = Field(default=None, ge=0)
    remaining_duration_minutes: int | None = Field(default=None, ge=0)
    actual_work_minutes: int | None = Field(default=None, ge=0)
    remaining_work_minutes: int | None = Field(default=None, ge=0)
    actual_start: datetime | None = None
    actual_finish: datetime | None = None

    @field_validator("actual_start", "actual_finish")
    @classmethod
    def project_local_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return _naive(value, info.field_name)

    @model_validator(mode="after")
    def valid_progress_update(self) -> TaskProgressUpdate:
        _require_kind(self.task, ObjectKind.TASK, "task")
        values = (
            self.percent_complete,
            self.actual_duration_minutes,
            self.remaining_duration_minutes,
            self.actual_work_minutes,
            self.remaining_work_minutes,
            self.actual_start,
            self.actual_finish,
        )
        if all(value is None for value in values):
            raise ValueError("task_progress requires at least one progress value")
        return self


class TimephasedWorkUpdate(ContractModel):
    op: Literal["timephased_work"] = "timephased_work"
    assignment: ObjectRef
    date: datetime
    actual_work_minutes: int = Field(ge=0)

    @field_validator("date")
    @classmethod
    def project_local_time(cls, value: datetime) -> datetime:
        checked = _naive(value, "date")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def assignment_kind_only(self) -> TimephasedWorkUpdate:
        _require_kind(self.assignment, ObjectKind.ASSIGNMENT, "assignment")
        return self


class SetStatusDate(ContractModel):
    op: Literal["set_status_date"] = "set_status_date"
    status_date: datetime

    @field_validator("status_date")
    @classmethod
    def project_local_time(cls, value: datetime) -> datetime:
        checked = _naive(value, "status_date")
        assert checked is not None
        return checked


StatusOperation = Annotated[
    TaskProgressUpdate | TimephasedWorkUpdate | SetStatusDate,
    Field(discriminator="op"),
]


class StatusRequest(ContractModel):
    project: ProjectRef
    expected_state: ProjectState
    idempotency_key: str = Field(min_length=8, max_length=128)
    mode: BatchMode
    updates: tuple[StatusOperation, ...] = Field(min_length=1, max_length=500)
    confirmation_token: str | None = None


class AnalyzeRequest(ContractModel):
    project: ProjectRef
    analysis: AnalysisKind
    baseline: int | None = Field(default=None, ge=0, le=10)


class ExportOptions(ContractModel):
    overwrite: bool = False
    include_headers: bool = True


class ExportRequest(ContractModel):
    project: ProjectRef
    format: Literal["pdf", "xlsx", "csv", "xml", "mpp"]
    destination: str
    expected_state: ProjectState
    idempotency_key: str = Field(min_length=8, max_length=128)
    mode: BatchMode
    confirmation_token: str | None = None
    options: ExportOptions = Field(default_factory=ExportOptions)


class DesktopProjectDetection(ContractModel):
    platform: str
    windows: bool
    com_registered: bool
    prog_ids: tuple[str, ...] = ()
    clsids: tuple[str, ...] = ()
    executable_paths: tuple[str, ...] = ()
    existing_executable_paths: tuple[str, ...] = ()
    version_hints: tuple[str, ...] = ()
    architecture_hints: tuple[str, ...] = ()
    pywin32_importable: bool
    pythoncom_importable: bool
    win32com_importable: bool
    probe_errors: tuple[str, ...] = ()
    activation_attempted: bool = False


class CapabilityReport(ContractModel):
    backend: str
    available: bool
    installed: bool
    contract_fidelity: ContractFidelity
    scheduling_fidelity: ContractFidelity
    desktop_smoke: DesktopSmoke
    activates_desktop: bool = False
    supported_tools: tuple[str, ...]
    supported_operations: tuple[str, ...]
    safety_classes: dict[str, str]
    notes: tuple[str, ...] = ()
    detection: DesktopProjectDetection | None = None


class ProjectSession(ContractModel):
    project: ProjectRef
    ownership: Ownership
    name: str
    path: str | None = None
    dirty: bool
    state: ProjectState


class QueryPage(ContractModel):
    project: ProjectRef
    entity: QueryEntity
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None = None
    state: ProjectState


class ChangePlan(ContractModel):
    plan_id: str
    request_family: str
    project: ProjectRef
    state_before: ProjectState
    operation_count: int
    destructive: bool
    confirmation_required: bool
    confirmation_token: str | None = None
    atomicity: Atomicity
    impact: dict[str, Any]
    expires_at: datetime
    commit_state: CommitState = CommitState.PLANNED


class ChangeReceipt(ContractModel):
    receipt_id: str
    project: ProjectRef
    idempotency_key: str
    state_before: ProjectState
    state_after: ProjectState
    requested: tuple[dict[str, Any], ...]
    observed: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()
    impact: dict[str, Any] = Field(default_factory=dict)
    verification: VerificationLevel
    atomicity: Atomicity
    undo_available: bool
    commit_state: CommitState
    replayed: bool = False
    committed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
