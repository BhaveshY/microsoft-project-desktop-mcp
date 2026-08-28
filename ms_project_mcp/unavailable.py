from __future__ import annotations

from typing import Any, NoReturn

from .backend import BackendQueryPage
from .detection import probe_desktop_project
from .errors import ErrorCode, MspError
from .mock import TOOL_NAMES
from .models import (
    AnalysisKind,
    Atomicity,
    CapabilityReport,
    ChangeReceipt,
    CloseDisposition,
    ContractFidelity,
    DesktopSmoke,
    DesktopProjectDetection,
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


class UnavailableProjectBackend:
    """Non-activating placeholder used until the live Project backend is available."""

    def __init__(
        self,
        reason: str = "Microsoft Project live backend is not implemented",
        *,
        detection: DesktopProjectDetection | None = None,
    ) -> None:
        self.reason = reason
        self.detection = detection or probe_desktop_project()

    def capabilities(self) -> CapabilityReport:
        return CapabilityReport(
            backend="unavailable",
            available=False,
            installed=self.detection.com_registered,
            contract_fidelity=ContractFidelity.CONTRACT_ONLY,
            scheduling_fidelity=ContractFidelity.CONTRACT_ONLY,
            desktop_smoke=DesktopSmoke.NOT_VERIFIED,
            activates_desktop=False,
            supported_tools=TOOL_NAMES,
            supported_operations=(),
            safety_classes={tool: "unavailable" for tool in TOOL_NAMES},
            notes=(self.reason, "Capabilities probing did not activate Microsoft Project or import pywin32"),
            detection=self.detection,
        )

    def _unavailable(self) -> NoReturn:
        raise MspError(ErrorCode.BACKEND_UNAVAILABLE, self.reason, retryable=False)

    def create_project(self, *, name: str, path: str | None) -> ProjectSession:
        self._unavailable()

    def open_project(self, *, path: str) -> ProjectSession:
        self._unavailable()

    def attach_project(self, *, name: str | None) -> ProjectSession:
        self._unavailable()

    def get_session(self, project: ProjectRef) -> ProjectSession:
        self._unavailable()

    def save_project(
        self, project: ProjectRef, *, path: str | None, expected_state: ProjectState
    ) -> ProjectSession:
        self._unavailable()

    def detach_project(self, project: ProjectRef) -> None:
        self._unavailable()

    def close_project(
        self, project: ProjectRef, disposition: CloseDisposition, *, expected_state: ProjectState
    ) -> None:
        self._unavailable()

    def query(
        self,
        project: ProjectRef,
        entity: QueryEntity,
        *,
        fields: tuple[str, ...],
        limit: int,
        offset: int,
    ) -> BackendQueryPage:
        self._unavailable()

    def current_state(self, project: ProjectRef) -> ProjectState:
        self._unavailable()

    def dependency_edges(self, project: ProjectRef) -> tuple[tuple[int, int], ...]:
        self._unavailable()

    def task_parent_edges(self, project: ProjectRef) -> tuple[tuple[int, int], ...]:
        self._unavailable()

    def resolve_ref(self, project: ProjectRef, ref: ObjectRef) -> int:
        self._unavailable()

    def apply_operations(
        self,
        project: ProjectRef,
        operations: tuple[Operation, ...],
        *,
        idempotency_key: str,
        verification: VerificationLevel,
        expected_state: ProjectState,
    ) -> ChangeReceipt:
        self._unavailable()

    def schedule(
        self,
        project: ProjectRef,
        command: ScheduleCommand,
        options: ScheduleOptions,
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]:
        self._unavailable()

    def update_status(
        self,
        project: ProjectRef,
        updates: tuple[StatusOperation, ...],
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]:
        self._unavailable()

    def analyze(self, project: ProjectRef, analysis: AnalysisKind, baseline: int | None) -> dict[str, Any]:
        self._unavailable()

    def export(
        self,
        project: ProjectRef,
        format: str,
        destination: str,
        options: ExportOptions,
        *,
        expected_state: ProjectState,
    ) -> dict[str, Any]:
        self._unavailable()

    def ownership(self, project: ProjectRef) -> Ownership:
        self._unavailable()

    def plan_atomicity(self, request_family: str, operations: tuple[Operation, ...] = ()) -> Atomicity:
        return Atomicity.NON_ATOMIC

    def shutdown(self) -> None:
        return None
