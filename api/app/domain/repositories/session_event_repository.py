from typing import Protocol

from datetime import datetime
from app.domain.models.session_event import SessionEvent


class SessionEventRepository(Protocol):
    """event 数据仓库"""

    async def add(self, event: SessionEvent) -> None:
        ...


    async def get_by_session_id(self, session_id: str, limit: int, before_created_at: datetime | None = None,) -> tuple[list[SessionEvent], bool]:
        ...


    async def get_by_event_id(self, event_id: str) -> SessionEvent | None:
        ...