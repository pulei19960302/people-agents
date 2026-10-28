from typing import Protocol
from datetime import datetime

from app.domain.models.run_checkpoint import RunCheckpoint
from app.domain.models.session_run import SessionRun, SessionRunStatus


class SessionRunRepository(Protocol):

    async def add(self, run: SessionRun) -> None:
        ...

    async def get_by_id(self, run_id: str) -> SessionRun | None:
        ...

    async def get_latest_by_session_id(
            self,
            session_id: str,
    ) -> SessionRun | None:
        ...

    async def get_latest_by_session_ids(
            self,
            session_ids: list[str],
    ) -> dict[str, SessionRun]:
        ...

    async def bind_task(
            self,
            run_id: str,
            task_id: str,
    ) -> None:
        ...

    async def clear_task(
            self,
            run_id: str,
            expected_task_id: str,
    ) -> None:
        ...

    async def update_status(
            self,
            run_id: str,
            status: SessionRunStatus,
            error: str | None = None,
    ) -> None:
        ...

    async def update_heartbeat(
            self,
            run_id: str,
            task_id: str,
            timestamp: datetime,
    ) -> None:
        ...

    async def update_checkpoint(
            self,
            run_id: str,
            checkpoint: RunCheckpoint,
    ) -> None:
        """原子更新当前步骤指针和流程恢复快照。"""
        ...

    async def interrupt_orphaned_runs(self) -> int:
        ...
