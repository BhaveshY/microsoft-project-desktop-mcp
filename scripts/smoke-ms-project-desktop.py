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
        CreateCalendar,
        CreateResource,
        CreateTask,
        DeleteTask,
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
        TaskConstraintType,
        TaskProgressUpdate,
        TaskType,
        TimephasedWorkUpdate,
        UpdateProjectProperties,
        UpdateResource,
        UpdateTask,
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
            CreateCalendar(client_ref="delivery-calendar", name="Delivery Calendar"),
            CreateTask(client_ref="summary", name="Desktop smoke", duration_minutes=0),
            CreateTask(
                client_ref="plan",
                name="Plan",
                duration_minutes=480,
                parent=ObjectRef(kind=ObjectKind.TASK, client_ref="summary"),
                constraint_type=TaskConstraintType.START_NO_EARLIER_THAN,
                constraint_date=datetime(2027, 1, 4, 8, 0),
                deadline=datetime(2027, 1, 15, 17, 0),
                task_type=TaskType.FIXED_DURATION,
                effort_driven=False,
                manual=False,
                priority=700,
                notes="Advanced planning controls verified",
                calendar=ObjectRef(kind=ObjectKind.CALENDAR, client_ref="delivery-calendar"),
                ignore_resource_calendar=True,
            ),
            CreateTask(
                client_ref="deliver",
                name="Deliver",
                duration_minutes=960,
                parent=ObjectRef(kind=ObjectKind.TASK, client_ref="summary"),
                after=ObjectRef(kind=ObjectKind.TASK, client_ref="plan"),
            ),
            CreateTask(client_ref="disposable", name="Disposable summary", duration_minutes=0),
            CreateTask(
                client_ref="disposable-child",
                name="Disposable child",
                parent=ObjectRef(kind=ObjectKind.TASK, client_ref="disposable"),
                deadline=datetime(2027, 1, 8, 17, 0),
                calendar=ObjectRef(
                    kind=ObjectKind.CALENDAR, client_ref="delivery-calendar"
                ),
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
                initials="SE",
                group="Delivery",
                code="SMOKE-01",
                email="smoke@example.com",
                notes="Advanced resource details verified",
                base_calendar=ObjectRef(kind=ObjectKind.CALENDAR, client_ref="delivery-calendar"),
            ),
            CreateAssignment(
                client_ref="plan-owner",
                task=ObjectRef(kind=ObjectKind.TASK, client_ref="plan"),
                resource=ObjectRef(kind=ObjectKind.RESOURCE, client_ref="engineer"),
                units_percent=Decimal("100"),
            ),
            UpdateProjectProperties(
                title="Microsoft Project MCP Desktop Smoke Verified",
                comments="Verified by the live Microsoft Project desktop smoke",
                author="Microsoft Project MCP",
                keywords="MCP, verification",
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

        stage = "modify"
        built_tasks = service.query(QueryRequest(project=session.project, entity=QueryEntity.TASK))
        built_resources = service.query(
            QueryRequest(project=session.project, entity=QueryEntity.RESOURCE)
        )
        disposable_child = next(
            item for item in built_tasks.items if item["name"] == "Disposable child"
        )
        engineer = next(
            item for item in built_resources.items if item["name"] == "Smoke Engineer"
        )
        receipt = service.apply(
            ApplyRequest(
                project=session.project,
                batch=OperationBatch(
                    operations=(
                        UpdateTask(
                            task=ObjectRef(
                                kind=ObjectKind.TASK,
                                unique_id=disposable_child["ref"]["unique_id"],
                            ),
                            clear_deadline=True,
                            clear_calendar=True,
                            notes="Updated before recursive deletion",
                        ),
                        UpdateResource(
                            resource=ObjectRef(
                                kind=ObjectKind.RESOURCE,
                                unique_id=engineer["ref"]["unique_id"],
                            ),
                            group="Delivery Operations",
                            email="delivery-smoke@example.com",
                        ),
                    ),
                    expected_state=receipt.state_after,
                    idempotency_key=_key("modify"),
                    mode=BatchMode.COMMIT,
                ),
            )
        )

        stage = "recursive_delete"
        disposable = next(item for item in built_tasks.items if item["name"] == "Disposable summary")
        delete_operations = (
            DeleteTask(
                task=ObjectRef(
                    kind=ObjectKind.TASK,
                    unique_id=disposable["ref"]["unique_id"],
                ),
                recursive=True,
            ),
        )
        delete_key = _key("recursive-delete")
        delete_plan = service.apply(
            ApplyRequest(
                project=session.project,
                batch=OperationBatch(
                    operations=delete_operations,
                    expected_state=receipt.state_after,
                    idempotency_key=delete_key,
                    mode=BatchMode.PLAN,
                ),
            )
        )
        receipt = service.apply(
            ApplyRequest(
                project=session.project,
                batch=OperationBatch(
                    operations=delete_operations,
                    expected_state=receipt.state_after,
                    idempotency_key=delete_key,
                    mode=BatchMode.COMMIT,
                    confirmation_token=delete_plan.confirmation_token,
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
                idempotency_key=_key("status-progress"),
                mode=BatchMode.COMMIT,
                updates=(
                    TaskProgressUpdate(
                        task=ObjectRef(kind=ObjectKind.TASK, unique_id=plan_task["ref"]["unique_id"]),
                        percent_complete=Decimal("25"),
                    ),
                ),
            )
        )
        service.status(
            StatusRequest(
                project=session.project,
                expected_state=backend.current_state(session.project),
                idempotency_key=_key("status-timephased"),
                mode=BatchMode.COMMIT,
                updates=(
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
        reopened_resources = service.query(
            QueryRequest(project=reopened.project, entity=QueryEntity.RESOURCE)
        )
        reopened_project = service.query(
            QueryRequest(project=reopened.project, entity=QueryEntity.PROJECT)
        )
        names = {item["name"] for item in reopened_tasks.items}
        if not {"Desktop smoke", "Plan", "Deliver"}.issubset(names):
            raise RuntimeError("reopened task names did not match")
        if len(reopened_dependencies.items) < 1 or len(reopened_assignments.items) < 1:
            raise RuntimeError("reopened dependencies or assignments were missing")
        reopened_plan = next(item for item in reopened_tasks.items if item["name"] == "Plan")
        reopened_engineer = next(
            item for item in reopened_resources.items if item["name"] == "Smoke Engineer"
        )
        project_item = reopened_project.items[0]
        if (
            reopened_plan["constraint_type_name"]
            != TaskConstraintType.START_NO_EARLIER_THAN.value
            or reopened_plan["constraint_date"] != datetime(2027, 1, 4, 8, 0).isoformat()
            or reopened_plan["deadline"] != datetime(2027, 1, 15, 17, 0).isoformat()
            or reopened_plan["task_type"] != TaskType.FIXED_DURATION.value
            or reopened_plan["effort_driven"] is not False
            or reopened_plan["manual"] is not False
            or reopened_plan["priority"] != 700
            or reopened_plan["calendar_ref"] is None
            or reopened_engineer["base_calendar_ref"] is None
            or reopened_engineer["code"] != "SMOKE-01"
            or reopened_engineer["group"] != "Delivery Operations"
            or reopened_engineer["email"] != "delivery-smoke@example.com"
            or project_item["calendar_ref"] is None
            or project_item["default_task_type"] != TaskType.FIXED_DURATION.value
            or project_item["new_tasks_manual"] is not False
            or project_item["multiple_critical_paths"] is not True
            or project_item["author"] != "Microsoft Project MCP"
            or project_item["keywords"] != "MCP, verification"
        ):
            raise RuntimeError("reopened advanced planning fields did not match")
        reopened_saved = service.project(
            ProjectRequest(
                action=ProjectAction.SAVE,
                project=reopened.project,
                expected_state=backend.current_state(reopened.project),
                idempotency_key=_key("reopen-save"),
            )
        )
        service.project(
            ProjectRequest(
                action=ProjectAction.CLOSE,
                project=reopened.project,
                expected_state=reopened_saved.state,
                idempotency_key=_key("reopen-close"),
                close_disposition=CloseDisposition.REFUSE_IF_DIRTY,
            )
        )
        open_refs.remove(reopened.project)

        stage = "template_create"
        template_fixture = fixture_dir / "desktop-smoke-from-template.mpp"
        templated = service.project(
            ProjectRequest(
                action=ProjectAction.CREATE,
                name="Microsoft Project MCP Template Smoke",
                path=str(template_fixture),
                template_path=str(fixture),
                idempotency_key=_key("template-create"),
            )
        )
        open_refs.append(templated.project)
        templated_tasks = service.query(
            QueryRequest(project=templated.project, entity=QueryEntity.TASK)
        )
        if "Plan" not in {item["name"] for item in templated_tasks.items}:
            raise RuntimeError("template-created project did not contain the source tasks")
        service.project(
            ProjectRequest(
                action=ProjectAction.CLOSE,
                project=templated.project,
                expected_state=backend.current_state(templated.project),
                idempotency_key=_key("template-close"),
                close_disposition=CloseDisposition.SAVE_AND_CLOSE,
            )
        )
        open_refs.remove(templated.project)
        _print(
            {
                "status": "VERIFIED",
                "fixture": str(fixture),
                "task_count": len(reopened_tasks.items),
                "dependency_count": len(reopened_dependencies.items),
                "assignment_count": len(reopened_assignments.items),
                "advanced_planning": True,
                "recursive_delete": True,
                "template_create": True,
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
