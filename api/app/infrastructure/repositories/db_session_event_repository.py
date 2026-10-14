from sqlalchemy import Select

from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models.session_event import SessionEvent
from app.domain.repositories.session_event_repository import SessionEventRepository
from app.infrastructure.models import SessionEventModel


class DBSessionEventRepository(SessionEventRepository):

    """数据库的事件session event"""

    def __init__(self, db_session: AsyncSession) -> None:
        self.db_session = db_session



    async def add(self, event: SessionEvent) -> None:

        record = SessionEventModel.from_domain(event)
        self.db_session.add(record)

    async def get_by_session_id(
            self,
            session_id: str,
            limit: int,
            before_created_at: datetime | None = None,
    ) -> tuple[list[SessionEvent], bool]:

        conditions = [
            SessionEventModel.session_id == session_id,
        ]

        if before_created_at is not None:
            conditions.append(
                SessionEventModel.created_at < before_created_at,
            )

        stmt = (
            Select(SessionEventModel)
            .where(*conditions)
            .order_by(SessionEventModel.created_at.desc())
            # 多查一条，用于判断是否还有下一页
            .limit(limit + 1)
        )

        result = await self.db_session.execute(stmt)
        records = list(result.scalars().all())


        has_more = len(records) > limit
        records = records[:limit]

        # 前端时间线需要旧 -> 新
        records.reverse()

        return (
            [record.to_domain() for record in records],
            has_more,
        )


    async def get_by_event_id(self, event_id: str) -> SessionEvent | None:
        stmt = (
            Select(SessionEventModel)
            .where(SessionEventModel.event_id == event_id)
        )

        result = await self.db_session.execute(stmt)

        record = result.scalars().one_or_none()

        return record.to_domain() if record is not None else None


