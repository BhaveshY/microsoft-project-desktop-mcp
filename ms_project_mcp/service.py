from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable

from .backend import ProjectBackend
from .errors import BackendExecutionError, DispatchState, ErrorCode, MspError
from .ledger import (
    InMemoryOperationLedger,
    LedgerEntry,
    LedgerState,
    OperationLedger,
    PlanRecord,
)
from .models import (
    AddDependency,
    AnalyzeRequest,
    ApplyRequest,
    Atomicity,
    BatchMode,
    ChangePlan,
    ChangeReceipt,
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
    ExportRequest,
    MoveTask,
    ObjectKind,
    ObjectRef,
    Operation,
    Ownership,
    ProjectAction,
    ProjectRef,
    ProjectRequest,
    ProjectSession,
    ProjectState,
    QueryPage,
    QueryRequest,
    RemoveDependency,
    ScheduleCommand,
    ScheduleRequest,
    SetBaseline,
    StatusRequest,
    UpdateAssignment,
    UpdateCalendar,
    UpdateProjectProperties,
    UpdateResource,
    UpdateTask,
)


PLAN_TTL = timedelta(minutes=10)


class ProjectService:
    def __init__(
        self,
        backend: ProjectBackend,
        *,
        ledger: OperationLedger | None = None,
        confirmation_secret: bytes | None = None,
        clock: Callable[[], datetime] | None = None,
        plan_ttl: timedelta = PLAN_TTL,
    ) -> None:
        self.backend = backend
        self.ledger = ledger or InMemoryOperationLedger()
        self._secret = confirmation_secret or secrets.token_bytes(32)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._plan_ttl = plan_ttl
        self._plan_counter = 0
        self._plan_lock = Lock()

    def capabilities(self) -> Any:
        return self.backend.capabilities()

    def shutdown(self) -> None:
        try:
            self.backend.shutdown()
        finally:
            self.ledger.close()

    def project(self, request: ProjectRequest) -> Any:
        if request.action == ProjectAction.CREATE:
            if request.name is None:
                raise MspError(ErrorCode.INVALID_REQUEST, "create requires name")
            return self._lifecycle_open(
                request,
                (lambda: self.backend.create_project(
                        name=request.name,
                        path=request.path,
                        template_path=request.template_path,
                    ))
                    if request.template_path is not None
                    else (lambda: self.backend.create_project(name=request.name, path=request.path)),
            )
        if request.action == ProjectAction.OPEN:
            if request.path is None:
                raise MspError(ErrorCode.INVALID_REQUEST, "open requires path")
            return self._lifecycle_open(request, lambda: self.backend.open_project(path=request.path))
        if request.action == ProjectAction.ATTACH:
            return self._lifecycle_open(request, lambda: self.backend.attach_project(name=request.name))
        if request.project is None:
            raise MspError(ErrorCode.INVALID_REQUEST, f"{request.action.value} requires project")
        if request.action == ProjectAction.DETACH:
            if self.backend.ownership(request.project) != Ownership.ATTACHED_USER_OWNED:
                raise MspError(ErrorCode.OWNERSHIP_VIOLATION, "Only user-owned projects may be detached")
            self.backend.detach_project(request.project)
            return {"detached": True, "project": request.project}

        self._require_lifecycle_write_fields(request)
        assert request.expected_state is not None
        assert request.idempotency_key is not None
        family = request.action.value
        fingerprint = self._fingerprint(
            family,
            request.idempotency_key,
            {
                "project": request.project.model_dump(mode="json"),
                "path": request.path,
                "close_disposition": request.close_disposition.value,
                "expected_state": request.expected_state.model_dump(mode="json"),
            },
        )
        replay = self._replay(request.project, request.idempotency_key, family, fingerprint)
        if replay is not None:
            return replay
        self._require_current_state(request.project, request.expected_state)

        if request.action == ProjectAction.SAVE:
            return self._dispatch(
                request.project,
                request.idempotency_key,
                family,
                fingerprint,
                lambda: self.backend.save_project(
                    request.project, path=request.path, expected_state=request.expected_state
                ),
            )
        if request.action == ProjectAction.CLOSE:
            if self.backend.ownership(request.project) == Ownership.ATTACHED_USER_OWNED:
                raise MspError(ErrorCode.OWNERSHIP_VIOLATION, "Attached user-owned projects are detach-only")
            if request.close_disposition == CloseDisposition.DISCARD_AND_CLOSE:
                if request.confirmation_token is None:
                    return self._issue_plan(
                        request_family=family,
                        project=request.project,
                        state=request.expected_state,
                        fingerprint=fingerprint,
                        operation_count=1,
                        destructive=True,
                        atomicity=self.backend.plan_atomicity(family),
                        impact={"action": "discard_unsaved_changes_and_close"},
                    )
                self._consume_confirmation(request.confirmation_token, fingerprint)
            return self._dispatch(
                request.project,
                request.idempotency_key,
                family,
                fingerprint,
                lambda: self._close(request),
            )
        raise MspError(ErrorCode.UNSUPPORTED_OPERATION, f"Unsupported project action: {request.action.value}")

    def _lifecycle_open(
        self,
        request: ProjectRequest,
        action: Callable[[], ProjectSession],
    ) -> ProjectSession:
        assert request.idempotency_key is not None
        family = f"project:{request.action.value}"
        scope = ProjectRef(session_id="msp-lifecycle", project_key="msp-lifecycle")
        fingerprint = self._fingerprint(
            family,
            request.idempotency_key,
            {
                "action": request.action.value,
                "name": request.name,
                "path": request.path,
                "template_path": request.template_path,
            },
        )
        result = self._dispatch(
            scope,
            request.idempotency_key,
            family,
            fingerprint,
            action,
        )
        if not isinstance(result, ProjectSession):
            raise MspError(ErrorCode.INTERNAL_ERROR, "Lifecycle ledger result has an unexpected type")
        try:
            return self.backend.get_session(result.project)
        except MspError as exc:
            if exc.code not in {
                ErrorCode.SESSION_NOT_FOUND,
                ErrorCode.PROJECT_CLOSED,
                ErrorCode.PROJECT_IDENTITY_CHANGED,
            }:
                raise
            raise MspError(
                ErrorCode.UNKNOWN_COMMIT_STATE,
                "The lifecycle action committed in an earlier process, but its desktop session cannot be resumed; inspect Project and use a new key",
                retryable=False,
                details={"action": request.action.value, "ledger_scope": scope.session_id},
            ) from exc

    def _close(self, request: ProjectRequest) -> dict[str, Any]:
        assert request.project is not None
        assert request.expected_state is not None
        self.backend.close_project(
            request.project, request.close_disposition, expected_state=request.expected_state
        )
        return {"closed": True, "project": request.project}

    def query(self, request: QueryRequest) -> QueryPage:
        offset = 0
        expected_state = self.backend.current_state(request.project)
        if request.cursor is not None:
            cursor = self._decode_cursor(request.cursor)
            expected_identity = {
                "project": request.project.model_dump(mode="json"),
                "entity": request.entity.value,
                "fields": list(request.fields),
            }
            if any(cursor.get(key) != value for key, value in expected_identity.items()):
                raise MspError(ErrorCode.INVALID_REQUEST, "Cursor does not match this query")
            cursor_state = ProjectState.model_validate(cursor.get("state"))
            self._require_current_state(request.project, cursor_state)
            expected_state = cursor_state
            offset = cursor.get("offset")
            if not isinstance(offset, int) or offset < 0:
                raise MspError(ErrorCode.INVALID_REQUEST, "Cursor offset is invalid")
        page = self.backend.query(
            request.project,
            request.entity,
            fields=request.fields,
            limit=request.limit,
            offset=offset,
        )
        if page.state != expected_state:
            raise MspError(ErrorCode.STALE_STATE, "Project changed while query page was being read", retryable=True)
        next_cursor = None
        if page.next_offset is not None:
            next_cursor = self._encode_cursor(
                {
                    "project": request.project.model_dump(mode="json"),
                    "entity": request.entity.value,
                    "fields": list(request.fields),
                    "state": page.state.model_dump(mode="json"),
                    "offset": page.next_offset,
                }
            )
        return QueryPage(
            project=request.project,
            entity=request.entity,
            items=page.items,
            next_cursor=next_cursor,
            state=page.state,
        )

    def apply(self, request: ApplyRequest) -> ChangePlan | ChangeReceipt:
        batch = request.batch
        family = "apply"
        fingerprint = self._fingerprint(
            family,
            batch.idempotency_key,
            {
                "project": request.project.model_dump(mode="json"),
                "expected_state": batch.expected_state.model_dump(mode="json"),
                "operations": [operation.model_dump(mode="json") for operation in batch.operations],
                "verification": batch.verification.value,
            },
        )
        replay = self._replay(request.project, batch.idempotency_key, family, fingerprint)
        if batch.mode == BatchMode.COMMIT and replay is not None:
            if isinstance(replay, ChangeReceipt):
                return replay.model_copy(update={"replayed": True})
            raise MspError(ErrorCode.INTERNAL_ERROR, "Apply ledger result has an unexpected type")
        self._require_current_state(request.project, batch.expected_state)
        self._validate_operations(request.project, batch.operations)
        destructive = any(
            isinstance(
                operation,
                (DeleteTask, DeleteResource, DeleteAssignment, DeleteCalendar, SetBaseline, ClearBaseline),
            )
            for operation in batch.operations
        )
        atomicity = self.backend.plan_atomicity(family, batch.operations)
        impact = {
            "operation_count": len(batch.operations),
            "operation_types": sorted({operation.op for operation in batch.operations}),
            "destructive": destructive,
        }
        if batch.mode == BatchMode.PLAN:
            return self._issue_plan(
                request_family=family,
                project=request.project,
                state=batch.expected_state,
                fingerprint=fingerprint,
                operation_count=len(batch.operations),
                destructive=destructive,
                atomicity=atomicity,
                impact=impact,
            )
        if destructive:
            if batch.confirmation_token is None:
                raise MspError(
                    ErrorCode.CONFIRMATION_REQUIRED,
                    "This batch requires a previously issued dry-run confirmation token",
                )
            self._consume_confirmation(batch.confirmation_token, fingerprint)
        return self._dispatch(
            request.project,
            batch.idempotency_key,
            family,
            fingerprint,
            lambda: self.backend.apply_operations(
                request.project,
                batch.operations,
                idempotency_key=batch.idempotency_key,
                verification=batch.verification,
                expected_state=batch.expected_state,
            ),
        )

    def schedule(self, request: ScheduleRequest) -> Any:
        confirmation_required = request.command in {
            ScheduleCommand.LEVEL,
            ScheduleCommand.CLEAR_LEVELING,
            ScheduleCommand.RESCHEDULE,
        }
        return self._generic_write(
            request_family="schedule",
            project=request.project,
            expected_state=request.expected_state,
            idempotency_key=request.idempotency_key,
            mode=request.mode,
            confirmation_token=request.confirmation_token,
            payload={"command": request.command.value, "options": request.options.model_dump(mode="json")},
            confirmation_required=confirmation_required,
            atomicity=self.backend.plan_atomicity("schedule"),
            dispatch=lambda: self.backend.schedule(
                request.project,
                request.command,
                request.options,
                expected_state=request.expected_state,
            ),
        )

    def status(self, request: StatusRequest) -> Any:
        for update in request.updates:
            ref = getattr(update, "task", None) or getattr(update, "assignment", None)
            if ref is not None:
                self.backend.resolve_ref(request.project, ref)
        return self._generic_write(
            request_family="status",
            project=request.project,
            expected_state=request.expected_state,
            idempotency_key=request.idempotency_key,
            mode=request.mode,
            confirmation_token=request.confirmation_token,
            payload={"updates": [update.model_dump(mode="json") for update in request.updates]},
            confirmation_required=False,
            atomicity=self.backend.plan_atomicity("status"),
            dispatch=lambda: self.backend.update_status(
                request.project, request.updates, expected_state=request.expected_state
            ),
        )

    def analyze(self, request: AnalyzeRequest) -> Any:
        return self.backend.analyze(request.project, request.analysis, request.baseline)

    def export(self, request: ExportRequest) -> Any:
        return self._generic_write(
            request_family="export",
            project=request.project,
            expected_state=request.expected_state,
            idempotency_key=request.idempotency_key,
            mode=request.mode,
            confirmation_token=request.confirmation_token,
            payload={
                "format": request.format,
                "destination": request.destination,
                "options": request.options.model_dump(mode="json"),
            },
            confirmation_required=True,
            atomicity=self.backend.plan_atomicity("export"),
            dispatch=lambda: self.backend.export(
                request.project,
                request.format,
                request.destination,
                request.options,
                expected_state=request.expected_state,
            ),
        )

    def _generic_write(
        self,
        *,
        request_family: str,
        project: ProjectRef,
        expected_state: ProjectState,
        idempotency_key: str,
        mode: BatchMode,
        confirmation_token: str | None,
        payload: dict[str, Any],
        confirmation_required: bool,
        atomicity: Atomicity,
        dispatch: Callable[[], Any],
    ) -> Any:
        fingerprint = self._fingerprint(
            request_family,
            idempotency_key,
            {
                "project": project.model_dump(mode="json"),
                "expected_state": expected_state.model_dump(mode="json"),
                "payload": payload,
            },
        )
        replay = self._replay(project, idempotency_key, request_family, fingerprint)
        if mode == BatchMode.COMMIT and replay is not None:
            return {"replayed": True, "result": replay}
        self._require_current_state(project, expected_state)
        if mode == BatchMode.PLAN:
            return self._issue_plan(
                request_family=request_family,
                project=project,
                state=expected_state,
                fingerprint=fingerprint,
                operation_count=1,
                destructive=confirmation_required,
                atomicity=atomicity,
                impact=payload,
            )
        if confirmation_required:
            if confirmation_token is None:
                raise MspError(
                    ErrorCode.CONFIRMATION_REQUIRED,
                    "This action requires a previously issued dry-run confirmation token",
                )
            self._consume_confirmation(confirmation_token, fingerprint)
        result = self._dispatch(project, idempotency_key, request_family, fingerprint, dispatch)
        return {"replayed": False, "result": result}

    def _dispatch(
        self,
        project: ProjectRef,
        idempotency_key: str,
        request_family: str,
        fingerprint: str,
        action: Callable[[], Any],
    ) -> Any:
        claim = self.ledger.claim_dispatch(
            LedgerEntry(
                session_id=project.session_id,
                idempotency_key=idempotency_key,
                request_family=request_family,
                fingerprint=fingerprint,
                state=LedgerState.PENDING_DISPATCH,
            )
        )
        entry = claim.entry
        if not claim.acquired:
            if entry.state == LedgerState.COMMITTED_RECEIPT:
                return entry.result
            self._raise_unresolved(entry)
        if entry.state == LedgerState.COMMITTED_RECEIPT:
            return entry.result
        if entry.state != LedgerState.PENDING_DISPATCH:
            self._raise_unresolved(entry)
        try:
            result = action()
        except BackendExecutionError as exc:
            if exc.dispatch_state == DispatchState.NOT_DISPATCHED:
                self.ledger.release_not_dispatched(project.session_id, idempotency_key, fingerprint)
            else:
                self.ledger.mark_unknown(project.session_id, idempotency_key, exc.dispatch_state.value)
            raise
        except MspError:
            self.ledger.release_not_dispatched(project.session_id, idempotency_key, fingerprint)
            raise
        except Exception as exc:
            self.ledger.mark_unknown(project.session_id, idempotency_key, type(exc).__name__)
            raise
        self.ledger.mark_committed(project.session_id, idempotency_key, result)
        return result

    def _replay(
        self,
        project: ProjectRef,
        idempotency_key: str,
        request_family: str,
        fingerprint: str,
    ) -> Any | None:
        entry = self.ledger.lookup(project.session_id, idempotency_key)
        if entry is None:
            return None
        if entry.fingerprint != fingerprint or entry.request_family != request_family:
            raise MspError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Idempotency key was used for another request",
                details={"existing_family": entry.request_family, "new_family": request_family},
            )
        if entry.state == LedgerState.COMMITTED_RECEIPT:
            return entry.result
        self._raise_unresolved(entry)

    @staticmethod
    def _raise_unresolved(entry: LedgerEntry) -> None:
        if entry.state == LedgerState.PENDING_DISPATCH:
            raise MspError(
                ErrorCode.REQUEST_IN_PROGRESS,
                "An identical request is already being dispatched",
                retryable=True,
                details={"ledger_state": entry.state.value, "request_family": entry.request_family},
            )
        raise MspError(
            ErrorCode.UNKNOWN_COMMIT_STATE,
            "Request has no safely replayable committed result and requires reconciliation",
            retryable=False,
            details={"ledger_state": entry.state.value, "request_family": entry.request_family},
        )

    def _issue_plan(
        self,
        *,
        request_family: str,
        project: ProjectRef,
        state: ProjectState,
        fingerprint: str,
        operation_count: int,
        destructive: bool,
        atomicity: Atomicity,
        impact: dict[str, Any],
    ) -> ChangePlan:
        now = self._clock()
        expires_at = now + self._plan_ttl
        with self._plan_lock:
            self._plan_counter += 1
            plan_sequence = self._plan_counter
        plan_id = f"plan-{fingerprint[:16]}-{plan_sequence:08d}"
        token = None
        if destructive:
            token = hmac.new(
                self._secret,
                f"{plan_id}:{fingerprint}:{expires_at.isoformat()}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            self.ledger.store_plan(
                PlanRecord(
                    plan_id=plan_id,
                    token=token,
                    fingerprint=fingerprint,
                    project=project,
                    state_before=state,
                    atomicity=atomicity,
                    expires_at=expires_at,
                )
            )
        return ChangePlan(
            plan_id=plan_id,
            request_family=request_family,
            project=project,
            state_before=state,
            operation_count=operation_count,
            destructive=destructive,
            confirmation_required=destructive,
            confirmation_token=token,
            atomicity=atomicity,
            impact=impact,
            expires_at=expires_at,
        )

    def _consume_confirmation(self, token: str, fingerprint: str) -> None:
        self.ledger.consume_plan(token, fingerprint, self._clock())

    def _require_lifecycle_write_fields(self, request: ProjectRequest) -> None:
        if request.expected_state is None or request.idempotency_key is None:
            raise MspError(ErrorCode.INVALID_REQUEST, "Lifecycle writes require expected_state and idempotency_key")

    def _require_current_state(self, project: ProjectRef, expected: ProjectState) -> None:
        current = self.backend.current_state(project)
        if current != expected:
            raise MspError(
                ErrorCode.STALE_STATE,
                "Project state changed since it was read",
                retryable=True,
                details={
                    "expected_state": expected.model_dump(mode="json"),
                    "current_state": current.model_dump(mode="json"),
                },
            )

    def _validate_operations(self, project: ProjectRef, operations: tuple[Operation, ...]) -> None:
        declared: dict[ObjectKind, set[str]] = {kind: set() for kind in ObjectKind}
        create_kinds = {
            "create_task": ObjectKind.TASK,
            "create_resource": ObjectKind.RESOURCE,
            "create_assignment": ObjectKind.ASSIGNMENT,
            "create_calendar": ObjectKind.CALENDAR,
        }
        for operation in operations:
            kind = create_kinds.get(operation.op)
            if kind is not None:
                client_ref = operation.client_ref
                if client_ref in declared[kind]:
                    raise MspError(ErrorCode.INVALID_REQUEST, f"Duplicate {kind.value} client_ref")
                declared[kind].add(client_ref)
        available: dict[ObjectKind, set[str]] = {kind: set() for kind in ObjectKind}

        def resolve(ref: ObjectRef | None) -> str | None:
            if ref is None:
                return None
            if ref.client_ref is not None:
                if ref.client_ref not in available[ref.kind]:
                    raise MspError(
                        ErrorCode.INVALID_REQUEST,
                        "Batch-local references may only target an earlier matching create operation",
                        details={"kind": ref.kind.value, "client_ref": ref.client_ref},
                    )
                return f"{ref.kind.value}:client:{ref.client_ref}"
            return f"{ref.kind.value}:uid:{self.backend.resolve_ref(project, ref)}"

        edges = {(f"task:uid:{pred}", f"task:uid:{succ}") for pred, succ in self.backend.dependency_edges(project)}
        parent_edges = {
            (f"task:uid:{child}", f"task:uid:{parent}")
            for child, parent in self.backend.task_parent_edges(project)
        }
        deleted_tasks: set[str] = set()
        for operation in operations:
            if isinstance(operation, CreateTask):
                parent_ref = resolve(operation.parent)
                resolve(operation.after)
                resolve(operation.calendar)
                available[ObjectKind.TASK].add(operation.client_ref)
                task_ref = f"task:client:{operation.client_ref}"
                if parent_ref is not None:
                    parent_edges.add((task_ref, parent_ref))
            elif isinstance(operation, (UpdateTask, DeleteTask)):
                task_ref = resolve(operation.task)
                if isinstance(operation, UpdateTask):
                    resolve(operation.calendar)
                if isinstance(operation, DeleteTask) and task_ref is not None:
                    deleted_tasks.add(task_ref)
            elif isinstance(operation, MoveTask):
                task_ref = resolve(operation.task)
                parent_ref = resolve(operation.parent)
                after_ref = resolve(operation.after)
                if task_ref in {parent_ref, after_ref}:
                    raise MspError(ErrorCode.INVALID_REQUEST, "A task cannot be moved relative to itself")
                assert task_ref is not None
                parent_edges = {edge for edge in parent_edges if edge[0] != task_ref}
                if parent_ref is not None:
                    parent_edges.add((task_ref, parent_ref))
            elif isinstance(operation, CreateResource):
                resolve(operation.base_calendar)
                available[ObjectKind.RESOURCE].add(operation.client_ref)
            elif isinstance(operation, (UpdateResource, DeleteResource)):
                resolve(operation.resource)
                if isinstance(operation, UpdateResource):
                    resolve(operation.base_calendar)
            elif isinstance(operation, CreateAssignment):
                resolve(operation.task)
                resolve(operation.resource)
                available[ObjectKind.ASSIGNMENT].add(operation.client_ref)
            elif isinstance(operation, (UpdateAssignment, DeleteAssignment)):
                resolve(operation.assignment)
            elif isinstance(operation, AddDependency):
                edge = (resolve(operation.predecessor), resolve(operation.successor))
                assert edge[0] is not None and edge[1] is not None
                if edge[0] == edge[1]:
                    raise MspError(ErrorCode.DEPENDENCY_CYCLE, "A task cannot depend on itself")
                if edge in edges:
                    raise MspError(ErrorCode.INVALID_REQUEST, "Duplicate dependency")
                edges.add(edge)
            elif isinstance(operation, RemoveDependency):
                edges.discard((resolve(operation.predecessor), resolve(operation.successor)))
            elif isinstance(operation, CreateCalendar):
                resolve(operation.base_calendar)
                available[ObjectKind.CALENDAR].add(operation.client_ref)
            elif isinstance(operation, (UpdateCalendar, DeleteCalendar)):
                resolve(operation.calendar)
            elif isinstance(operation, UpdateProjectProperties):
                resolve(operation.calendar)
        edges = {edge for edge in edges if edge[0] not in deleted_tasks and edge[1] not in deleted_tasks}
        parent_edges = {
            edge for edge in parent_edges if edge[0] not in deleted_tasks and edge[1] not in deleted_tasks
        }
        if self._has_cycle(edges):
            raise MspError(ErrorCode.DEPENDENCY_CYCLE, "Dependency changes would create a cycle")
        if self._has_cycle(parent_edges):
            raise MspError(ErrorCode.INVALID_REQUEST, "Task hierarchy changes would create a cycle")

    @staticmethod
    def _has_cycle(edges: set[tuple[str, str]]) -> bool:
        graph: dict[str, set[str]] = {}
        for predecessor, successor in edges:
            graph.setdefault(predecessor, set()).add(successor)
            graph.setdefault(successor, set())
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(visit(next_node) for next_node in graph.get(node, ())):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        return any(visit(node) for node in graph)

    @staticmethod
    def _fingerprint(request_family: str, idempotency_key: str, payload: Any) -> str:
        normalized = {"family": request_family, "idempotency_key": idempotency_key, "payload": payload}
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _encode_cursor(self, payload: dict[str, Any]) -> str:
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        return f"v1.{encoded}.{signature}"

    def _decode_cursor(self, cursor: str) -> dict[str, Any]:
        try:
            version, encoded, signature = cursor.split(".", 2)
            expected = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
            if version != "v1" or not hmac.compare_digest(signature, expected):
                raise ValueError
            padded = encoded + "=" * (-len(encoded) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MspError(ErrorCode.INVALID_REQUEST, "Cursor is invalid or has been tampered with") from exc
