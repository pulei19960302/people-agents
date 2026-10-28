from typing import Protocol

from datetime import datetime
from app.domain.models.session_event import SessionEvent


class SessionEventRepository(Protocol):
    """event 数据仓库"""

    async def add(self, event: SessionEvent) -> None:
        ...

    async def count_tool_calls_by_run(self, run_id: str) -> int:
        """统计此Run已发出的工具调用，包含此前恢复执行的调用。"""
        ...

    async def count_started_steps_by_run(self, run_id: str) -> int:
        """统计此Run已开始的步骤，保证任务恢复后不重置步骤预算。"""
        ...


    async def get_by_session_id(self, session_id: str, limit: int, before_created_at: datetime | None = None,) -> tuple[list[SessionEvent], bool]:
        ...


    async def get_by_event_id(self, event_id: str) -> SessionEvent | None:
        ...

    async def get_last_plan(self, session_id: str, run_id: str) -> SessionEvent | None:
        ...

    async def get_run_page_by_session_id(
            self,
            session_id: str,
            run_limit: int,
            before_started_at: datetime | None = None,
            before_run_id: str | None = None,
    ) -> tuple[
        list[SessionEvent],
        bool,
        datetime | None,
        str | None
    ]:
        ...
