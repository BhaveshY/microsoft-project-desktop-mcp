from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Event, Lock

from ms_project_mcp.errors import BackendExecutionError, DispatchState, ErrorCode, MspError
from ms_project_mcp.ledger import LedgerState
from ms_project_mcp.mock import MockProjectBackend
from ms_project_mcp.models import (
    BatchMode,
    ProjectAction,
    ProjectRequest,
    ScheduleCommand,
    ScheduleRequest,
    ScheduleOptions,
)
from ms_project_mcp.service import ProjectService


class _CountingBackend(MockProjectBackend):
    def __init__(self) -> None:
        super().__init__()
        self.dispatch_count = 0
        self.dispatch_started = Event()
        self.release_dispatch = Event()
        self._count_lock = Lock()

    def schedule(self, project, command, options, *, expected_state):
        with self._count_lock:
            self.dispatch_count += 1
        self.dispatch_started.set()
        if not self.release_dispatch.wait(timeout=5):
            raise RuntimeError("test dispatch release timed out")
        return super().schedule(project, command, options, expected_state=expected_state)


class ConcurrentIdempotencyTests(unittest.TestCase):
    def _request(self, session, *, key: str, command: ScheduleCommand = ScheduleCommand.CALCULATE):
        return ScheduleRequest(
            project=session.project,
            command=command,
            expected_state=session.state,
            idempotency_key=key,
            mode=BatchMode.COMMIT,
        )

    def test_concurrent_identical_commits_have_exactly_one_dispatcher(self) -> None:
        backend = _CountingBackend()
        service = ProjectService(backend, confirmation_secret=b"concurrent-secret")
        session = service.project(ProjectRequest(action=ProjectAction.CREATE, name="Concurrency", idempotency_key="create-concurrency-0001"))
        request = self._request(session, key="concurrent-same-0001")
        start = Barrier(3)

        def invoke():
            start.wait(timeout=5)
            try:
                return ("ok", service.schedule(request))
            except MspError as exc:
                return ("error", exc.code)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(invoke)
            second = executor.submit(invoke)
            start.wait(timeout=5)
            self.assertTrue(backend.dispatch_started.wait(timeout=5))
            backend.release_dispatch.set()
            results = (first.result(timeout=5), second.result(timeout=5))

        self.assertEqual(backend.dispatch_count, 1)
        self.assertIn("ok", {result[0] for result in results})
        errors = [result[1] for result in results if result[0] == "error"]
        self.assertTrue(not errors or errors == [ErrorCode.REQUEST_IN_PROGRESS])
        entry = service.ledger.lookup(session.project.session_id, request.idempotency_key)
        self.assertEqual(entry.state, LedgerState.COMMITTED_RECEIPT)

    def test_concurrent_different_payload_with_same_key_is_a_collision(self) -> None:
        backend = _CountingBackend()
        service = ProjectService(backend, confirmation_secret=b"collision-secret")
        session = service.project(ProjectRequest(action=ProjectAction.CREATE, name="Collision", idempotency_key="create-collision-0001"))
        first_request = self._request(session, key="concurrent-drift-0001")
        different_request = ScheduleRequest(
            project=session.project,
            command=ScheduleCommand.CALCULATE,
            expected_state=session.state,
            idempotency_key="concurrent-drift-0001",
            mode=BatchMode.COMMIT,
            options=ScheduleOptions(status_date=datetime(2028, 1, 1)),
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(service.schedule, first_request)
            self.assertTrue(backend.dispatch_started.wait(timeout=5))
            with self.assertRaises(MspError) as collision:
                service.schedule(different_request)
            self.assertEqual(collision.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
            backend.release_dispatch.set()
            first.result(timeout=5)

        self.assertEqual(backend.dispatch_count, 1)

    def test_not_dispatched_failure_releases_claim_for_safe_retry(self) -> None:
        class RetryableBackend(MockProjectBackend):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def schedule(self, project, command, options, *, expected_state):
                self.attempts += 1
                if self.attempts == 1:
                    raise BackendExecutionError(
                        "COM worker was unavailable before enqueue",
                        dispatch_state=DispatchState.NOT_DISPATCHED,
                    )
                return super().schedule(project, command, options, expected_state=expected_state)

        backend = RetryableBackend()
        service = ProjectService(backend, confirmation_secret=b"safe-retry-secret")
        session = service.project(ProjectRequest(action=ProjectAction.CREATE, name="Retry", idempotency_key="create-retry-0001"))
        request = self._request(session, key="safe-retry-0001")
        with self.assertRaises(BackendExecutionError) as first:
            service.schedule(request)
        self.assertEqual(first.exception.dispatch_state, DispatchState.NOT_DISPATCHED)
        self.assertIsNone(service.ledger.lookup(session.project.session_id, request.idempotency_key))

        result = service.schedule(request)
        self.assertFalse(result["replayed"])
        self.assertEqual(backend.attempts, 2)
        entry = service.ledger.lookup(session.project.session_id, request.idempotency_key)
        self.assertEqual(entry.state, LedgerState.COMMITTED_RECEIPT)

    def test_uncertain_failure_blocks_replay_until_reconciliation(self) -> None:
        class UncertainBackend(MockProjectBackend):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def schedule(self, project, command, options, *, expected_state):
                self.attempts += 1
                raise BackendExecutionError(
                    "Connection was lost after COM invocation",
                    dispatch_state=DispatchState.MAY_HAVE_DISPATCHED,
                )

        backend = UncertainBackend()
        service = ProjectService(backend, confirmation_secret=b"uncertain-secret")
        session = service.project(ProjectRequest(action=ProjectAction.CREATE, name="Uncertain", idempotency_key="create-uncertain-0001"))
        request = self._request(session, key="uncertain-retry-0001")
        with self.assertRaises(BackendExecutionError) as first:
            service.schedule(request)
        self.assertEqual(first.exception.dispatch_state, DispatchState.MAY_HAVE_DISPATCHED)
        entry = service.ledger.lookup(session.project.session_id, request.idempotency_key)
        self.assertEqual(entry.state, LedgerState.UNKNOWN_COMMIT_STATE)

        with self.assertRaises(MspError) as replay:
            service.schedule(request)
        self.assertEqual(replay.exception.code, ErrorCode.UNKNOWN_COMMIT_STATE)
        self.assertEqual(backend.attempts, 1)

    def test_ordinary_backend_validation_error_does_not_poison_idempotency_key(self) -> None:
        class ValidatingBackend(MockProjectBackend):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def schedule(self, project, command, options, *, expected_state):
                self.attempts += 1
                if self.attempts == 1:
                    raise MspError(ErrorCode.UNSUPPORTED_OPERATION, "Command is disabled")
                return super().schedule(project, command, options, expected_state=expected_state)

        backend = ValidatingBackend()
        service = ProjectService(backend, confirmation_secret=b"validation-secret")
        session = service.project(ProjectRequest(action=ProjectAction.CREATE, name="Validation", idempotency_key="create-validation-0001"))
        request = self._request(session, key="validation-retry-0001")
        with self.assertRaises(MspError) as first:
            service.schedule(request)
        self.assertEqual(first.exception.code, ErrorCode.UNSUPPORTED_OPERATION)
        self.assertIsNone(service.ledger.lookup(session.project.session_id, request.idempotency_key))
        service.schedule(request)
        self.assertEqual(backend.attempts, 2)

    def test_uncertain_create_is_not_dispatched_twice(self) -> None:
        class UncertainCreateBackend(MockProjectBackend):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def create_project(self, *, name, path):
                self.attempts += 1
                super().create_project(name=name, path=path)
                raise BackendExecutionError(
                    "Create completed but the response was lost",
                    dispatch_state=DispatchState.MAY_HAVE_DISPATCHED,
                )

        backend = UncertainCreateBackend()
        service = ProjectService(backend, confirmation_secret=b"create-uncertain-secret")
        request = ProjectRequest(
            action=ProjectAction.CREATE,
            name="Uncertain lifecycle",
            idempotency_key="uncertain-create-0001",
        )
        with self.assertRaises(BackendExecutionError):
            service.project(request)
        with self.assertRaises(MspError) as replay:
            service.project(request)
        self.assertEqual(replay.exception.code, ErrorCode.UNKNOWN_COMMIT_STATE)
        self.assertEqual(backend.attempts, 1)


if __name__ == "__main__":
    unittest.main()
