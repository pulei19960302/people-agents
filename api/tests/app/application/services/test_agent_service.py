import asyncio
from dataclasses import dataclass, field

from app.application.services.agent_service import AgentService
from app.domain.models.session_run import SessionRun, SessionRunStatus


@dataclass
class RunStore:
    runs: list[SessionRun] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class FakeSessionRepository:
    def __init__(self, store: RunStore, uow: "FakeUnitOfWork") -> None:
        self._store = store
        self._uow = uow

    async def lock_for_run_resolution(self, session_id: str) -> bool:
        await self._store.lock.acquire()
        self._uow.has_lock = True
        return True


class FakeSessionRunRepository:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    async def get_latest_by_session_id(
            self,
            session_id: str,
    ) -> SessionRun | None:
        # 主动让出执行权，保证测试覆盖两个请求同时进入决策的情况。
        await asyncio.sleep(0)
        matching_runs = [
            run for run in self._store.runs
            if run.session_id == session_id
        ]
        return matching_runs[-1] if matching_runs else None

    async def get_by_id(self, run_id: str) -> SessionRun | None:
        return next(
            (run for run in self._store.runs if run.id == run_id),
            None,
        )

    async def add(self, run: SessionRun) -> None:
        self._store.runs.append(run)

    async def update_status(self, run_id: str, status: SessionRunStatus) -> None:
        run = await self.get_by_id(run_id)
        assert run is not None
        run.status = status


class FakeUnitOfWork:
    def __init__(self, store: RunStore) -> None:
        self._store = store
        self.has_lock = False
        self.session = FakeSessionRepository(store, self)
        self.session_run = FakeSessionRunRepository(store)

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self.has_lock:
            self._store.lock.release()
        return False


def test_resolve_run_serializes_concurrent_new_runs() -> None:
    async def run_test() -> None:
        session_id = "session-1"
        store = RunStore(
            runs=[
                SessionRun(
                    session_id=session_id,
                    status=SessionRunStatus.COMPLETED,
                )
            ]
        )

        service = object.__new__(AgentService)
        service._uow_factory = lambda: FakeUnitOfWork(store)

        results = await asyncio.gather(
            service._resolve_run(session_id),
            service._resolve_run(session_id),
            return_exceptions=True,
        )

        created_runs = [
            result for result in results
            if isinstance(result, SessionRun)
        ]
        errors = [
            result for result in results
            if isinstance(result, RuntimeError)
        ]

        assert len(created_runs) == 1
        assert len(errors) == 1
        assert "当前任务正在运行" in str(errors[0])
        assert len(store.runs) == 2
        assert store.runs[-1].status == SessionRunStatus.PENDING

    asyncio.run(run_test())


def test_resolve_run_rejects_non_latest_run() -> None:
    async def run_test() -> None:
        session_id = "session-1"
        older_run = SessionRun(
            session_id=session_id,
            status=SessionRunStatus.FAILED,
        )
        latest_run = SessionRun(
            session_id=session_id,
            status=SessionRunStatus.COMPLETED,
        )
        store = RunStore(runs=[older_run, latest_run])

        service = object.__new__(AgentService)
        service._uow_factory = lambda: FakeUnitOfWork(store)

        try:
            await service._resolve_run(
                session_id=session_id,
                resume_run_id=older_run.id,
            )
        except RuntimeError as exc:
            assert "只能恢复最新运行" in str(exc)
        else:
            raise AssertionError("恢复旧Run时应当抛出RuntimeError")

    asyncio.run(run_test())


def test_plain_chat_creates_new_run_after_interruption() -> None:
    async def run_test() -> None:
        old_run = SessionRun(
            session_id="session-1",
            status=SessionRunStatus.INTERRUPTED,
        )
        store = RunStore(runs=[old_run])
        service = object.__new__(AgentService)
        service._uow_factory = lambda: FakeUnitOfWork(store)

        new_run = await service._resolve_run("session-1")
        assert new_run.id != old_run.id
        assert new_run.status == SessionRunStatus.PENDING
        assert old_run.status == SessionRunStatus.INTERRUPTED

    asyncio.run(run_test())


def test_plain_chat_cancels_waiting_run_before_new_run() -> None:
    async def run_test() -> None:
        old_run = SessionRun(session_id="session-1", status=SessionRunStatus.WAITING)
        store = RunStore(runs=[old_run])
        service = object.__new__(AgentService)
        service._uow_factory = lambda: FakeUnitOfWork(store)

        new_run = await service._resolve_run("session-1")
        assert new_run.id != old_run.id
        assert old_run.status == SessionRunStatus.CANCELLED
        assert new_run.status == SessionRunStatus.PENDING

    asyncio.run(run_test())


def test_explicit_resume_keeps_waiting_run() -> None:
    async def run_test() -> None:
        old_run = SessionRun(session_id="session-1", status=SessionRunStatus.WAITING)
        store = RunStore(runs=[old_run])
        service = object.__new__(AgentService)
        service._uow_factory = lambda: FakeUnitOfWork(store)

        resumed = await service._resolve_run("session-1", resume_run_id=old_run.id)
        assert resumed.id == old_run.id
        assert resumed.status == SessionRunStatus.WAITING
        assert len(store.runs) == 1

    asyncio.run(run_test())
