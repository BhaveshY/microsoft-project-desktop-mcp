from __future__ import annotations

import importlib
import queue
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel

from .compat import StrEnum


T = TypeVar("T")


class StaRuntime(Protocol):
    coinit_apartmentthreaded: int
    coinit_disable_ole1dde: int

    def initialize(self, flags: int) -> None: ...

    def pump_waiting_messages(self) -> None: ...

    def uninitialize(self) -> None: ...


class StaHostState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"


class StaHostClosedError(RuntimeError):
    pass


class StaWorkerFailedError(RuntimeError):
    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"STA worker failed: {type(cause).__name__}")
        self.cause_type = type(cause).__name__


class StaCallTimeout(TimeoutError):
    """Waiting timed out; `future` remains the sole submitted dispatch."""

    def __init__(self, future: Future[Any]) -> None:
        super().__init__("STA call timed out while the original dispatch continues")
        self.future = future


class StaBoundaryError(TypeError):
    pass


@dataclass(frozen=True)
class _WorkItem:
    function: Callable[[], Any]
    future: Future[Any]


_STOP = object()


class _PyWin32Runtime:
    def __init__(self) -> None:
        self._pythoncom = importlib.import_module("pythoncom")
        self.coinit_apartmentthreaded = int(self._pythoncom.COINIT_APARTMENTTHREADED)
        self.coinit_disable_ole1dde = int(getattr(self._pythoncom, "COINIT_DISABLE_OLE1DDE", 0))

    def initialize(self, flags: int) -> None:
        self._pythoncom.CoInitializeEx(flags)

    def pump_waiting_messages(self) -> None:
        self._pythoncom.PumpWaitingMessages()

    def uninitialize(self) -> None:
        self._pythoncom.CoUninitialize()


def load_pywin32_runtime() -> StaRuntime:
    """Load pywin32 lazily. This function is called only by the owned worker thread."""
    return _PyWin32Runtime()


def ensure_value_object(value: Any) -> None:
    """Reject arbitrary objects, including raw COM proxies, at the STA boundary."""
    primitives = (type(None), bool, int, float, str, bytes, Decimal, date, datetime, time, UUID, Enum)
    if isinstance(value, primitives):
        return
    if isinstance(value, BaseModel):
        ensure_value_object(value.model_dump(mode="python"))
        return
    if is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            ensure_value_object(getattr(value, item.name))
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            ensure_value_object(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, (str, int)):
                raise StaBoundaryError("STA result dictionaries require string or integer keys")
            ensure_value_object(item)
        return
    raise StaBoundaryError(f"STA result contains a non-value object: {type(value).__name__}")


class StaHost:
    """Single-threaded apartment executor for internal Microsoft Project adapter calls."""

    def __init__(
        self,
        *,
        runtime_factory: Callable[[], StaRuntime] = load_pywin32_runtime,
        pump_interval: float = 0.02,
        result_validator: Callable[[Any], None] = ensure_value_object,
        thread_name: str = "ms-project-sta",
    ) -> None:
        if pump_interval <= 0:
            raise ValueError("pump_interval must be positive")
        self._runtime_factory = runtime_factory
        self._pump_interval = pump_interval
        self._result_validator = result_validator
        self._thread_name = thread_name
        self._queue: queue.Queue[_WorkItem | object] = queue.Queue()
        self._state = StaHostState.NEW
        self._state_lock = threading.Lock()
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._owner_thread_id: int | None = None
        self._runtime: StaRuntime | None = None

    @property
    def state(self) -> StaHostState:
        with self._state_lock:
            return self._state

    @property
    def owner_thread_id(self) -> int | None:
        return self._owner_thread_id

    def start(self, timeout: float | None = 5.0) -> None:
        with self._state_lock:
            if self._state != StaHostState.NEW:
                raise StaHostClosedError(f"STA host cannot start from state {self._state.value}")
            self._state = StaHostState.STARTING
            self._thread = threading.Thread(target=self._worker, name=self._thread_name, daemon=True)
            self._thread.start()
        if not self._started.wait(timeout):
            raise TimeoutError("STA worker did not initialize before the start timeout")
        if self._startup_error is not None:
            raise self._startup_error
        if self.state != StaHostState.RUNNING:
            raise StaHostClosedError(f"STA host did not enter running state: {self.state.value}")

    def submit(self, function: Callable[[], T]) -> Future[T]:
        with self._state_lock:
            if self._state != StaHostState.RUNNING or self._startup_error is not None:
                raise StaHostClosedError(f"STA host does not accept work in state {self._state.value}")
            future: Future[T] = Future()
            self._queue.put(_WorkItem(function=function, future=future))
            return future

    def call(self, function: Callable[[], T], timeout: float | None = None) -> T:
        future = self.submit(function)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            raise StaCallTimeout(future) from exc

    def pump_current_thread(self) -> None:
        """Pump once from code already executing on the owned STA thread."""
        if threading.get_ident() != self._owner_thread_id or self._runtime is None:
            raise RuntimeError("STA messages can only be pumped by the owning worker")
        self._runtime.pump_waiting_messages()

    def shutdown(self, timeout: float | None = 5.0) -> None:
        with self._state_lock:
            if self._state == StaHostState.NEW:
                self._state = StaHostState.STOPPED
                self._stopped.set()
                return
            if self._state == StaHostState.STOPPED:
                return
            if self._state in {StaHostState.STARTING, StaHostState.RUNNING}:
                self._state = StaHostState.SHUTTING_DOWN
                self._queue.put(_STOP)
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("STA worker did not stop before the shutdown timeout")

    def _worker(self) -> None:
        runtime: StaRuntime | None = None
        initialized = False
        try:
            self._owner_thread_id = threading.get_ident()
            runtime = self._runtime_factory()
            self._runtime = runtime
            flags = runtime.coinit_apartmentthreaded | runtime.coinit_disable_ole1dde
            runtime.initialize(flags)
            initialized = True
            with self._state_lock:
                if self._state == StaHostState.STARTING:
                    self._state = StaHostState.RUNNING
            self._started.set()
            while True:
                runtime.pump_waiting_messages()
                try:
                    item = self._queue.get(timeout=self._pump_interval)
                except queue.Empty:
                    continue
                if item is _STOP:
                    break
                assert isinstance(item, _WorkItem)
                if not item.future.set_running_or_notify_cancel():
                    continue
                try:
                    runtime.pump_waiting_messages()
                    result = item.function()
                    self._result_validator(result)
                except BaseException as exc:
                    item.future.set_exception(exc)
                else:
                    item.future.set_result(result)
                finally:
                    runtime.pump_waiting_messages()
        except BaseException as exc:
            self._startup_error = StaWorkerFailedError(exc)
            self._started.set()
        finally:
            failure = self._startup_error or StaHostClosedError("STA worker stopped before queued work completed")
            while True:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(pending, _WorkItem) and not pending.future.done():
                    pending.future.set_exception(failure)
            if initialized and runtime is not None:
                runtime.uninitialize()
            self._runtime = None
            with self._state_lock:
                self._state = StaHostState.STOPPED
            self._stopped.set()
