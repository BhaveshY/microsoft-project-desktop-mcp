from __future__ import annotations

import os
from typing import Callable, Mapping

from .backend import ProjectBackend
from .detection import probe_desktop_project
from .live import LiveProjectBackend
from .mock import MockProjectBackend
from .models import DesktopProjectDetection
from .persistence import load_or_create_secret, resolve_state_dir
from .service import ProjectService
from .sqlite_ledger import SQLiteOperationLedger
from .unavailable import UnavailableProjectBackend


def create_backend(
    environment: Mapping[str, str] | None = None,
    *,
    detection: DesktopProjectDetection | None = None,
    live_backend_factory: Callable[[DesktopProjectDetection], ProjectBackend] | None = None,
) -> ProjectBackend:
    env = os.environ if environment is None else environment
    selection = env.get("MSP_MCP_BACKEND", "auto").strip().lower()
    if selection == "mock":
        return MockProjectBackend()
    if selection not in {"", "auto", "live"}:
        return UnavailableProjectBackend(f"Unsupported MSP_MCP_BACKEND selection: {selection}")
    observed = detection or probe_desktop_project()
    ready = observed.windows and observed.com_registered and observed.pywin32_importable
    if ready:
        factory = live_backend_factory or (lambda value: LiveProjectBackend(detection=value))
        return factory(observed)
    return UnavailableProjectBackend(
        "Microsoft Project desktop or pywin32 is unavailable; set MSP_MCP_BACKEND=mock for contract tests",
        detection=observed,
    )


def create_service(
    environment: Mapping[str, str] | None = None,
    *,
    detection: DesktopProjectDetection | None = None,
    live_backend_factory: Callable[[DesktopProjectDetection], ProjectBackend] | None = None,
) -> ProjectService:
    env = os.environ if environment is None else environment
    state_dir = resolve_state_dir(env)
    ledger = SQLiteOperationLedger(state_dir / "operation-ledger.sqlite3")
    secret = load_or_create_secret(state_dir)
    return ProjectService(
        create_backend(env, detection=detection, live_backend_factory=live_backend_factory),
        ledger=ledger,
        confirmation_secret=secret,
    )
