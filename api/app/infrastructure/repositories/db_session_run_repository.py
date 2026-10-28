from datetime import datetime

from sqlalchemy import select, update, func

from app.domain.models.session_run import SessionRun, utc_now, SessionRunStatus
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models.run_checkpoint import RunCheckpoint
from app.domain.repositories.session_run_repository import SessionRunRepository
from app.infrastructure.models import SessionRunModel


class DBSessionRunRepository(SessionRunRepository):

    def __init__(self, db_session: AsyncSession) -> None:
        self.db_session = db_session

    async def add(self, run: SessionRun) -> None:
        self.db_session.add(SessionRunModel.from_domain(run))


    async def get_by_id(self, run_id: str) -> SessionRun | None:

        stmt = (
            select(SessionRunModel)
            .where(
                SessionRunModel.id == run_id
            )
        )
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()
        return record.to_domain() if record else None


    async def get_latest_by_session_id(self, session_id: str) -> SessionRun | None:
        stmt = (
            select(SessionRunModel)
            .where(
                SessionRunModel.session_id == session_id
            )
            .order_by(
                SessionRunModel.created_at.desc(),
                SessionRunModel.id.desc(),
            )
            .limit(1)
        )
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()
        return record.to_domain() if record else None

    async def get_latest_by_session_ids(
            self,
            session_ids: list[str],
    ) -> dict[str, SessionRun]:
        """批量查询每个会话最新的一次运行。"""
        if not session_ids:
            return {}

        # PostgreSQL会将DISTINCT ON应用于session_id，并按照后续排序列
        # 为每个会话保留最新的一条记录。
        stmt = (
            select(SessionRunModel)
            .where(SessionRunModel.session_id.in_(session_ids))
            .distinct(SessionRunModel.session_id)
            .order_by(
                SessionRunModel.session_id,
                SessionRunModel.created_at.desc(),
                SessionRunModel.id.desc(),
            )
        )
        result = await self.db_session.execute(stmt)
        records = result.scalars().all()

        return {
            record.session_id: record.to_domain()
            for record in records
        }

    async def bind_task(self, run_id: str, task_id: str) -> None:
        now = utc_now()

        stmt = (
            update(SessionRunModel)
            .where(
                SessionRunModel.id == run_id,
                SessionRunModel.active_task_id.is_(None),
            )
            .values(
                active_task_id=task_id,
                status=SessionRunStatus.RUNNING.value,
                attempt_count=SessionRunModel.attempt_count + 1,
                started_at=func.coalesce(
                    SessionRunModel.started_at,
                    now,
                ),
                heartbeat_at=now,
                finished_at=None,
                error=None,
                updated_at=now,
            )
        )

        result = await self.db_session.execute(stmt)
        if result.rowcount != 1:
            raise RuntimeError(
                f"运行[{run_id}]已经绑定其他任务，不能重复启动"
            )

    async def clear_task(
            self,
            run_id: str,
            expected_task_id: str,
    ) -> None:
        stmt = (
            update(SessionRunModel)
            .where(
                SessionRunModel.id == run_id,
                SessionRunModel.active_task_id == expected_task_id,
            )
            .values(
                active_task_id=None,
                heartbeat_at=None,
                updated_at=utc_now(),
            )
        )
        await self.db_session.execute(stmt)

    async def update_status(
            self,
            run_id: str,
            status: SessionRunStatus,
            error: str | None = None,
    ) -> None:
        now = utc_now()
        values = {
            "status": status.value,
            "error": error,
            "updated_at": now,
        }

        if status in {
            SessionRunStatus.COMPLETED,
            SessionRunStatus.FAILED,
            SessionRunStatus.CANCELLED,
        }:
            values["finished_at"] = now

        stmt = (
            update(SessionRunModel)
            .where(SessionRunModel.id == run_id)
            .values(**values)
        )
        result = await self.db_session.execute(stmt)
        if result.rowcount != 1:
            raise ValueError(f"运行[{run_id}]不存在")

    async def update_heartbeat(
            self,
            run_id: str,
            task_id: str,
            timestamp: datetime,
    ) -> None:
        stmt = (
            update(SessionRunModel)
            .where(
                SessionRunModel.id == run_id,
                SessionRunModel.active_task_id == task_id,
                SessionRunModel.status == SessionRunStatus.RUNNING.value,
            )
            .values(
                heartbeat_at=timestamp,
                updated_at=timestamp,
            )
        )
        await self.db_session.execute(stmt)

    async def update_checkpoint(
            self,
            run_id: str,
            checkpoint: RunCheckpoint,
    ) -> None:
        stmt = (
            update(SessionRunModel)
            .where(SessionRunModel.id == run_id)
            .values(
                current_step_id=checkpoint.current_step_id,
                checkpoint=checkpoint.model_dump(mode="json"),
                updated_at=utc_now(),
            )
        )
        result = await self.db_session.execute(stmt)
        if result.rowcount != 1:
            raise ValueError(f"运行[{run_id}]不存在")

    async def interrupt_orphaned_runs(self) -> int:
        """中断服务重启后已经失去进程内执行实例的运行。"""
        stmt = (
            update(SessionRunModel)
            .where(
                SessionRunModel.status.in_([
                    SessionRunStatus.PENDING.value,
                    SessionRunStatus.RUNNING.value,
                ]),
            )
            .values(
                status=SessionRunStatus.INTERRUPTED.value,
                active_task_id=None,
                heartbeat_at=None,
                error="服务重启，原任务执行实例已经丢失",
                updated_at=utc_now(),
            )
        )
        result = await self.db_session.execute(stmt)
        return result.rowcount
