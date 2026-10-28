from sqlalchemy import select, or_, and_, func

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

    async def count_tool_calls_by_run(self, run_id: str) -> int:
        stmt = select(func.count()).select_from(SessionEventModel).where(
            SessionEventModel.run_id == run_id,
            SessionEventModel.event_type == "tool",
            SessionEventModel.payload["status"].astext == "calling",
        )
        result = await self.db_session.execute(stmt)
        return result.scalar_one()

    async def count_started_steps_by_run(self, run_id: str) -> int:
        stmt = select(func.count()).select_from(SessionEventModel).where(
            SessionEventModel.run_id == run_id,
            SessionEventModel.event_type == "step",
            SessionEventModel.payload["status"].astext == "started",
        )
        result = await self.db_session.execute(stmt)
        return result.scalar_one()

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
            select(SessionEventModel)
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
            select(SessionEventModel)
            .where(SessionEventModel.event_id == event_id)
        )

        result = await self.db_session.execute(stmt)

        record = result.scalars().one_or_none()

        return record.to_domain() if record is not None else None


    async def get_last_plan(self, session_id: str, run_id: str) -> SessionEvent | None:

        stmt = (
            select(SessionEventModel)
            .where(
                SessionEventModel.session_id == session_id,
                SessionEventModel.run_id == run_id,
                SessionEventModel.event_type == "plan"
            )
            .order_by(SessionEventModel.created_at.desc())
            .limit(1)
        )

        result = await self.db_session.execute(stmt)
        record = result.scalars().one_or_none()
        return record.to_domain() if record is not None else None




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
        str | None]:
        """
            按照 Agent 运行分页，返回本页所有运行的完整事件。

               返回值依次为：

               1. 本页所有完整运行的事件，按时间从旧到新排列；
               2. 是否还有更早的运行；
               3. 本页最老运行的开始时间；
               4. 本页最老运行的 run_id。

               before_started_at 和 before_run_id 必须同时存在或同时为空。
        """

        # 定义一次运行的开始时间。
        #
        # 一个run包含多条session_event。这里使用该run最早一条事件的
        # created_at作为运行开始时间。

        run_started_at_expression = func.min(
            SessionEventModel.created_at
        )

        # 构造第一阶段查询。
        #
        # 这一阶段只查询run_id及其开始时间，不查询具体事件。
        # run_id为空的数据不会进入新的运行分页接口。
        run_statement = (
            select(
                SessionEventModel.run_id.label("run_id"), # label 别名
                run_started_at_expression.label("run_started_at"),
            )
            .where(
                SessionEventModel.session_id == session_id,
                SessionEventModel.run_id.is_not(None),
            )
            .group_by(SessionEventModel.run_id)
        )

        # 如果携带游标，只查询游标之前的运行。
        #
        # 排序规则是：
        #   run_started_at DESC
        #   run_id DESC
        #
        # 因此，比游标更早的数据应满足：
        #   开始时间更早
        # 或
        #   开始时间相同，但run_id更小

        # 只有当提供了“前一个游标”（即上一页最后一条数据的信息）时，才应用此过滤
        if (
            before_started_at is not None
            and before_run_id is not None
        ):
            # having 过滤组
            run_statement = run_statement.having(
                or_(
                    # 情况 A：当前组的开始时间 早于 前一个游标的开始时间
                    # 这意味着当前组在时间轴上位于前一个组的“后面”（假设是倒序排列，取更早的时间即为下一页）
                    run_started_at_expression < before_started_at,
                    and_(
                        # 情况 B：当前组的开始时间 等于 前一个游标的开始时间
                        run_started_at_expression== before_started_at,
                        # 且 当前组的 run_id 小于 前一个游标的 run_id
                        # 这是为了处理“时间相同”时的并列情况，确保顺序唯一且连续
                        SessionEventModel.run_id < before_run_id,
                    )

                )
            )

        # 按照运行开始时间从新到旧排序。
        #
        # 多查询一个run，用于判断是否还有下一页。
        run_statement = (
            run_statement
            .order_by(
                run_started_at_expression.desc(),
                SessionEventModel.run_id.desc(),
            )
            .limit(run_limit + 1)
        )

        # 执行第一阶段查询。
        run_result = await self.db_session.execute(run_statement)
        run_rows = list(run_result.all())

        # 判断是否还有更早的运行。
        #
        # 假设run_limit=5：
        # - 查询到5条：没有证据表明存在下一页；
        # - 查询到6条：第6条说明还有下一页。
        has_more = len(run_rows) > run_limit

        # 8.删除用于判断has_more的额外记录。
        selected_run_rows = run_rows[:run_limit]

        if not selected_run_rows:
            return [], False, None, None

        # 提取当前页需要查询的run_id。
        #
        # 上面已经排除了NULL，因此这里拿到的都是字符串。

        selected_run_ids = [
            row.run_id
            for row in selected_run_rows
            if row.run_id is not None
        ]

        # 记录本页最老的运行。
        #
        # 第一阶段按照新到旧排序，所以最后一项就是本页最老的run。
        # Service会使用这两个值生成next_cursor。
        oldest_run_row = selected_run_rows[-1]
        oldest_run_started_at: datetime = (
            oldest_run_row.run_started_at
        )

        oldest_run_id: str = oldest_run_row.run_id

        # 构造第二阶段查询。
        #
        # 查询选中的所有run_id对应的完整事件。
        # 这里不能添加limit，否则一个run仍有可能被截断。

        event_statement = (
            select(SessionEventModel).where(
                SessionEventModel.session_id == session_id,
                SessionEventModel.run_id.in_(selected_run_ids)
            )
            .order_by(
                SessionEventModel.created_at.asc(),
                SessionEventModel.id.asc(),
            )
        )

        # 执行第二阶段查询。
        event_result = await self.db_session.execute(
            event_statement
        )
        event_records = list(event_result.scalars().all())

        # 将ORM模型转换成领域模型。
        #
        # 返回顺序是事件发生时间从旧到新，前端可以直接追加到timeline。
        events = [
            record.to_domain()
            for record in event_records
        ]

        # 返回完整事件和下一页游标所需的信息。
        return (
            events,
            has_more,
            oldest_run_started_at,
            oldest_run_id,
        )
