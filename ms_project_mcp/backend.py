from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .models import (
    AnalysisKind,
    Atomicity,
    CapabilityReport,
    ChangeReceipt,
    CloseDisposition,
    ExportOptions,
    ObjectRef,
    Operation,
    Ownership,
    ProjectRef,
    ProjectSession,
    ProjectState,
    QueryEntity,
    ScheduleCommand,
    ScheduleOptions,
    StatusOperation,
    VerificationLevel,
)


@dataclass(frozen=True)
class BackendQueryPage:
    items: tuple[dict[str, Any], ...]
    next_offset: int | None
    state: ProjectState


@runtime_checkable
class ProjectBackend(Protocol):
    """Deep boundary for both native Project automation and contract fakes."""

    def capabilities(self) -> CapabilityReport: ...

    def create_project(self, *, name: str, path: str | None) -> ProjectSession: ...

    def open_project(self, *, path: str) -> ProjectSession: ...

    def attach_project(self, *, name: str | None) -> ProjectSession: ...

    def get_session(self, project: ProjectRef) -> ProjectSession: ...

    def save_project(
        self, project: ProjectRef, *, path: str | None, expected_state: ProjectState
    ) -> ProjectSession: ...

    def detach_project(self, project: ProjectRef) -> None: ...

    def close_project(
        self, project: ProjectRef, disposition: CloseDisposition, *, expected_state: ProjectState
    ) -> None: ...

    def query(
        self,
        project: ProjectRef,
        entity: QueryEntity,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int,
    ) -> BackendQueryPage: ...

    def current_state(self, project: ProjectRef) -> ProjectState: ...

    def dependency_edges(self, project: ProjectRef) -> tuple[tuple[int, int], ...]: ...

    def task_parent_edges(self, project: ProjectRef) -> tuple[tuple[int, int], ...]: ...

    def resolve_ref(self, project: ProjectRef, ref: ObjectRef) -> int | str: ...

    def apply_operations(
        self,
        project: ProjectRef,
        operations: tuple[Operation, ...],
        *,
        idempotency_key: str,
        verification: VerificationLevel,
        expected_state: ProjectState,
    ) -> ChangeReceipt: ...

    def schedule(
        self,
        project: ProjectRef,
        command: ScheduleCommand,
        options: ScheduleOptions,
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]: ...

    def update_status(
        self,
        project: ProjectRef,
        updates: tuple[StatusOperation, ...],
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]: ...

    def analyze(self, project: ProjectRef, analysis: AnalysisKind, baseline: int | None) -> dict[str, Any]: ...

    def export(
        self,
        project: ProjectRef,
        format: str,
        destination: str,
        options: ExportOptions,
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]: ...

    def ownership(self, project: ProjectRef) -> Ownership: ...

    def plan_atomicity(self, request_family: str, operations: tuple[Operation, ...] = ()) -> Atomicity: ...

    def shutdown(self) -> None: ...
