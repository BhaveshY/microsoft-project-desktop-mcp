from __future__ import annotations

from threading import Lock
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from .errors import ErrorCode, MspError
from .factory import create_service
from .models import (
    AnalyzeRequest,
    ApplyRequest,
    ExportRequest,
    ProjectRequest,
    QueryRequest,
    ScheduleRequest,
    StatusRequest,
)
from .service import ProjectService


mcp = FastMCP(
    "Microsoft Project",
    instructions="Manage Microsoft Project through grouped, typed, state-checked operations.",
)
service: ProjectService | None = None
_service_lock = Lock()


def _service() -> ProjectService:
    """Create persistent state lazily; importing and tool discovery stay read-only."""
    global service
    if service is not None:
        return service
    with _service_lock:
        if service is None:
            service = create_service()
        return service


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _call(action: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "result": _jsonable(action())}
    except MspError as exc:
        return {"ok": False, "error": exc.as_dict()}
    except Exception as exc:
        error = MspError(ErrorCode.INTERNAL_ERROR, "Unhandled Microsoft Project MCP error")
        return {"ok": False, "error": {**error.as_dict(), "details": {"type": type(exc).__name__}}}


@mcp.tool(name="msp_capabilities")
def msp_capabilities() -> dict[str, Any]:
    """Report backend readiness and safety without launching Microsoft Project."""
    return _call(_service().capabilities)


@mcp.tool(name="msp_project")
def msp_project(request: ProjectRequest) -> dict[str, Any]:
    """Create, open, attach, save, detach, or close a project under explicit ownership rules."""
    return _call(lambda: _service().project(request))


@mcp.tool(name="msp_query")
def msp_query(request: QueryRequest) -> dict[str, Any]:
    """Read a compact, paginated projection using stable object references."""
    return _call(lambda: _service().query(request))


@mcp.tool(name="msp_apply")
def msp_apply(request: ApplyRequest) -> dict[str, Any]:
    """Plan or commit a typed, state-checked batch of project changes."""
    return _call(lambda: _service().apply(request))


@mcp.tool(name="msp_schedule")
def msp_schedule(request: ScheduleRequest) -> dict[str, Any]:
    """Plan or run an allowlisted native scheduling command."""
    return _call(lambda: _service().schedule(request))


@mcp.tool(name="msp_status")
def msp_status(request: StatusRequest) -> dict[str, Any]:
    """Plan or commit status and actuals updates."""
    return _call(lambda: _service().status(request))


@mcp.tool(name="msp_analyze")
def msp_analyze(request: AnalyzeRequest) -> dict[str, Any]:
    """Analyze schedule health through an allowlisted analysis kind."""
    return _call(lambda: _service().analyze(request))


@mcp.tool(name="msp_export")
def msp_export(request: ExportRequest) -> dict[str, Any]:
    """Plan or create an allowlisted report or export."""
    return _call(lambda: _service().export(request))


def main() -> None:
    try:
        mcp.run(transport="stdio")
    finally:
        if service is not None:
            service.shutdown()


if __name__ == "__main__":
    main()
