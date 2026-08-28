from __future__ import annotations

import argparse
import json
import tempfile
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, default=str))


def _key(label: str) -> str:
    return f"desktop-smoke-{label}-{uuid.uuid4().hex}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit Microsoft Project desktop write smoke")
    parser.add_argument(
        "--allow-write-fixture",
        action="store_true",
        help="authorize launching Project and writing one unique temporary MPP fixture",
    )
    args = parser.parse_args()
    if not args.allow_write_fixture:
        _print(
            {
                "status": "NOT_VERIFIED",
                "reason": "write_fixture_consent_required",
                "activation_attempted": False,
                "fixture_created": False,
            }
        )
        return 3

    # Keep all imports that can reach the live adapter after the consent gate.
    from ms_project_mcp.errors import MspError
    from ms_project_mcp.factory import create_backend
    from ms_project_mcp.ledger import InMemoryOperationLedger
    from ms_project_mcp.models import (
        AddDependency,
        ApplyRequest,
        BatchMode,
        CloseDisposition,
        CreateAssignment,
        CreateResource,
        CreateTask,
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
        StatusRequest,
        TaskProgressUpdate,
        TimephasedWorkUpdate,
    )
    from ms_project_mcp.service import ProjectService

    backend = create_backend({"MSP_MCP_BACKEND": "live"})
    capabilities = backend.capabilities()
    if not capabilities.available:
        _print(
            {
                "status": "NOT_VERIFIED",
                "reason": "microsoft_project_desktop_unavailable",
                "activation_attempted": False,
                "fixture_created": False,
                "detection": capabilities.model_dump(mode="json"),
            }
        )
        return 4

    fixture_dir = Path(tempfile.mkdtemp(prefix="ms-project-mcp-smoke-"))
    fixture = fixture_dir / "desktop-smoke.mpp"
    service = ProjectService(
        backend,
        ledger=InMemoryOperationLedger(),
        confirmation_secret=uuid.uuid4().bytes + uuid.uuid4().bytes,
    )
    open_refs: list[Any] = []
    stage = "create"
    try:
        session = service.project(
            ProjectRequest(action=ProjectAction.CREATE, name="Microsoft Project MCP Desktop Smoke", path=str(fixture), idempotency_key=_key("create"))
        )
        open_refs.append(session.project)

        stage = "build"
        operations = (
            CreateTask(client_ref="summary", name="Desktop smoke", duration_minutes=0),
            CreateTask(
                client_ref="plan",
                name="Plan",
                duration_minutes=480,
                parent=ObjectRef(kind=ObjectKind.TASK, client_ref="summary"),
            ),
            CreateTask(
                client_ref="deliver",
                name="Deliver",
                duration_minutes=960,
                parent=ObjectRef(kind=ObjectKind.TASK, client_ref="summary"),
                after=ObjectRef(kind=ObjectKind.TASK, client_ref="plan"),
            ),
            AddDependency(
                predecessor=ObjectRef(kind=ObjectKind.TASK, client_ref="plan"),
                successor=ObjectRef(kind=ObjectKind.TASK, client_ref="deliver"),
            ),
            CreateResource(
                client_ref="engineer",
                name="Smoke Engineer",
                resource_type=ResourceType.WORK,
                max_units_percent=Decimal("100"),
            ),
            CreateAssignment(
                client_ref="plan-owner",
                task=ObjectRef(kind=ObjectKind.TASK, client_ref="plan"),
                resource=ObjectRef(kind=ObjectKind.RESOURCE, client_ref="engineer"),
                units_percent=Decimal("100"),
            ),
        )
        receipt = service.apply(
            ApplyRequest(
                project=session.project,
                batch=OperationBatch(
                    operations=operations,
                    expected_state=session.state,
                    idempotency_key=_key("build"),
                    mode=BatchMode.COMMIT,
                ),
            )
        )

        stage = "calculate"
        service.schedule(
            ScheduleRequest(
                project=session.project,
                command=ScheduleCommand.CALCULATE,
                expected_state=receipt.state_after,
                idempotency_key=_key("calculate"),
                mode=BatchMode.COMMIT,
            )
        )

        stage = "status"
        tasks = service.query(QueryRequest(project=session.project, entity=QueryEntity.TASK))
        assignments = service.query(QueryRequest(project=session.project, entity=QueryEntity.ASSIGNMENT))
        plan_task = next(item for item in tasks.items if item["name"] == "Plan")
        assignment = assignments.items[0]
        start_value = plan_task.get("start")
        work_day = datetime.fromisoformat(start_value) if start_value else datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
        service.status(
            StatusRequest(
                project=session.project,
                expected_state=backend.current_state(session.project),
                idempotency_key=_key("status"),
                mode=BatchMode.COMMIT,
                updates=(
                    TaskProgressUpdate(
                        task=ObjectRef(kind=ObjectKind.TASK, unique_id=plan_task["ref"]["unique_id"]),
                        percent_complete=Decimal("25"),
                    ),
                    TimephasedWorkUpdate(
                        assignment=ObjectRef(
                            kind=ObjectKind.ASSIGNMENT,
                            unique_id=assignment["ref"]["unique_id"],
                        ),
                        date=work_day.replace(tzinfo=None),
                        actual_work_minutes=120,
                    ),
                ),
            )
        )

        stage = "save_close"
        saved = service.project(
            ProjectRequest(
                action=ProjectAction.SAVE,
                project=session.project,
                expected_state=backend.current_state(session.project),
                idempotency_key=_key("save"),
            )
        )
        service.project(
            ProjectRequest(
                action=ProjectAction.CLOSE,
                project=session.project,
                expected_state=saved.state,
                idempotency_key=_key("close"),
                close_disposition=CloseDisposition.REFUSE_IF_DIRTY,
            )
        )
        open_refs.remove(session.project)

        stage = "reopen_verify"
        reopened = service.project(ProjectRequest(action=ProjectAction.OPEN, path=str(fixture), idempotency_key=_key("open")))
        open_refs.append(reopened.project)
        reopened_tasks = service.query(QueryRequest(project=reopened.project, entity=QueryEntity.TASK))
        reopened_dependencies = service.query(
            QueryRequest(project=reopened.project, entity=QueryEntity.DEPENDENCY)
        )
        reopened_assignments = service.query(
            QueryRequest(project=reopened.project, entity=QueryEntity.ASSIGNMENT)
        )
        names = {item["name"] for item in reopened_tasks.items}
        if not {"Desktop smoke", "Plan", "Deliver"}.issubset(names):
            raise RuntimeError("reopened task names did not match")
        if len(reopened_dependencies.items) < 1 or len(reopened_assignments.items) < 1:
            raise RuntimeError("reopened dependencies or assignments were missing")
        service.project(
            ProjectRequest(
                action=ProjectAction.CLOSE,
                project=reopened.project,
                expected_state=reopened_tasks.state,
                idempotency_key=_key("reopen-close"),
                close_disposition=CloseDisposition.REFUSE_IF_DIRTY,
            )
        )
        open_refs.remove(reopened.project)
        _print(
            {
                "status": "VERIFIED",
                "fixture": str(fixture),
                "task_count": len(reopened_tasks.items),
                "dependency_count": len(reopened_dependencies.items),
                "assignment_count": len(reopened_assignments.items),
            }
        )
        return 0
    except Exception as exc:
        details = exc.as_dict() if isinstance(exc, MspError) else {"type": type(exc).__name__, "message": str(exc)}
        _print(
            {
                "status": "FAILED",
                "stage": stage,
                "fixture": str(fixture),
                "error": details,
            }
        )
        return 1
    finally:
        for project_ref in list(reversed(open_refs)):
            try:
                backend.close_project(
                    project_ref,
                    CloseDisposition.DISCARD_AND_CLOSE,
                    expected_state=backend.current_state(project_ref),
                )
            except Exception:
                pass
        try:
            backend.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
