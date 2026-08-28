from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

from ms_project_mcp.sta import (
    StaBoundaryError,
    StaCallTimeout,
    StaHost,
    StaHostClosedError,
    StaHostState,
    StaWorkerFailedError,
)


class _FakeRuntime:
    coinit_apartmentthreaded = 0x2
    coinit_disable_ole1dde = 0x4

    def __init__(self) -> None:
        self.initialize_flags: list[int] = []
        self.initialize_threads: list[int] = []
        self.uninitialize_threads: list[int] = []
        self.pump_threads: list[int] = []
        self._condition = threading.Condition()

    def initialize(self, flags: int) -> None:
        self.initialize_flags.append(flags)
        self.initialize_threads.append(threading.get_ident())

    def pump_waiting_messages(self) -> None:
        with self._condition:
            self.pump_threads.append(threading.get_ident())
            self._condition.notify_all()

    def uninitialize(self) -> None:
        self.uninitialize_threads.append(threading.get_ident())

    def wait_for_pumps(self, count: int, timeout: float = 2.0) -> bool:
        deadline = monotonic() + timeout
        with self._condition:
            while len(self.pump_threads) < count:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


class StaHostTests(unittest.TestCase):
    def _host(self, runtime: _FakeRuntime, **kwargs) -> StaHost:
        return StaHost(runtime_factory=lambda: runtime, pump_interval=0.005, **kwargs)

    def test_initializes_sta_flags_pumps_and_uninitializes_exactly_once(self) -> None:
        runtime = _FakeRuntime()
        host = self._host(runtime)
        host.start()
        self.assertTrue(runtime.wait_for_pumps(2))
        owner = host.owner_thread_id
        before = len(runtime.pump_threads)
        self.assertEqual(host.call(lambda: {"thread": threading.get_ident()}), {"thread": owner})
        self.assertTrue(runtime.wait_for_pumps(before + 2))
        host.shutdown()

        self.assertEqual(runtime.initialize_flags, [0x6])
        self.assertEqual(runtime.initialize_threads, [owner])
        self.assertEqual(runtime.uninitialize_threads, [owner])
        self.assertTrue(runtime.pump_threads)
        self.assertEqual(set(runtime.pump_threads), {owner})
        self.assertEqual(host.state, StaHostState.STOPPED)

    def test_concurrent_submission_uses_one_thread_and_serial_execution(self) -> None:
        runtime = _FakeRuntime()
        host = self._host(runtime)
        host.start()
        active = 0
        max_active = 0
        guard = threading.Lock()
        worker_threads: set[int] = set()

        def operation(index: int):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            worker_threads.add(threading.get_ident())
            with guard:
                active -= 1
            return index

        with ThreadPoolExecutor(max_workers=8) as callers:
            results = list(callers.map(lambda index: host.call(lambda: operation(index)), range(40)))
        host.shutdown()
        self.assertEqual(sorted(results), list(range(40)))
        self.assertEqual(worker_threads, {host.owner_thread_id})
        self.assertEqual(max_active, 1)

    def test_queue_order_is_serialized(self) -> None:
        runtime = _FakeRuntime()
        host = self._host(runtime)
        host.start()
        first_started = threading.Event()
        release_first = threading.Event()
        order: list[str] = []

        def first():
            order.append("first-start")
            first_started.set()
            release_first.wait(timeout=2)
            order.append("first-end")
            return "first"

        def second():
            order.append("second")
            return "second"

        first_future = host.submit(first)
        self.assertTrue(first_started.wait(timeout=2))
        second_future = host.submit(second)
        self.assertFalse(second_future.done())
        release_first.set()
        self.assertEqual(first_future.result(timeout=2), "first")
        self.assertEqual(second_future.result(timeout=2), "second")
        host.shutdown()
        self.assertEqual(order, ["first-start", "first-end", "second"])

    def test_exception_propagates_and_worker_remains_usable(self) -> None:
        runtime = _FakeRuntime()
        host = self._host(runtime)
        host.start()

        def fail():
            raise ValueError("bad adapter conversion")

        with self.assertRaisesRegex(ValueError, "bad adapter conversion"):
            host.call(fail)
        self.assertEqual(host.call(lambda: "still-running"), "still-running")
        host.shutdown()

    def test_timeout_keeps_original_future_without_duplicate_dispatch(self) -> None:
        runtime = _FakeRuntime()
        host = self._host(runtime)
        host.start()
        release = threading.Event()
        calls = 0

        def slow():
            nonlocal calls
            calls += 1
            release.wait(timeout=2)
            return "completed"

        with self.assertRaises(StaCallTimeout) as timeout:
            host.call(slow, timeout=0.01)
        self.assertFalse(timeout.exception.future.cancelled())
        release.set()
        self.assertEqual(timeout.exception.future.result(timeout=2), "completed")
        self.assertEqual(calls, 1)
        host.shutdown()

    def test_result_validator_rejects_proxy_like_objects_at_boundary(self) -> None:
        class ComLikeProxy:
            pass

        runtime = _FakeRuntime()
        host = self._host(runtime)
        host.start()
        with self.assertRaises(StaBoundaryError):
            host.call(ComLikeProxy)
        self.assertEqual(host.call(lambda: {"safe": [1, "two", None]}), {"safe": [1, "two", None]})
        host.shutdown()

    def test_shutdown_pairs_uninitialize_and_rejects_new_work(self) -> None:
        runtime = _FakeRuntime()
        host = self._host(runtime)
        with self.assertRaises(StaHostClosedError):
            host.submit(lambda: "too-early")
        host.start()
        host.shutdown()
        host.shutdown()
        self.assertEqual(len(runtime.uninitialize_threads), 1)
        with self.assertRaises(StaHostClosedError):
            host.submit(lambda: "late")
        with self.assertRaises(StaHostClosedError):
            host.start()

    def test_fatal_runtime_failure_completes_every_queued_future(self) -> None:
        class FailingPumpRuntime(_FakeRuntime):
            def __init__(self) -> None:
                super().__init__()
                self.fail_pump = threading.Event()

            def pump_waiting_messages(self) -> None:
                if self.fail_pump.is_set():
                    raise RuntimeError("message pump failed")
                super().pump_waiting_messages()

        runtime = FailingPumpRuntime()
        host = self._host(runtime)
        host.start()
        first_started = threading.Event()
        release_first = threading.Event()

        def first():
            first_started.set()
            release_first.wait(timeout=2)
            return "first-finished"

        first_future = host.submit(first)
        self.assertTrue(first_started.wait(timeout=2))
        queued = [host.submit(lambda index=index: index) for index in range(5)]
        runtime.fail_pump.set()
        release_first.set()
        self.assertEqual(first_future.result(timeout=2), "first-finished")
        for future in queued:
            with self.assertRaises(StaWorkerFailedError):
                future.result(timeout=2)
        self.assertEqual(host.state, StaHostState.STOPPED)
        self.assertEqual(len(runtime.uninitialize_threads), 1)


if __name__ == "__main__":
    unittest.main()
