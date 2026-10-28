from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models.agent_context_summary import AgentContextSummary
from app.domain.models.agent_message import AgentMessage
from app.infrastructure.models import AgentContextSummaryModel
from app.infrastructure.models.agent_message import AgentMessageModel
from app.domain.repositories.agent_context_repository import AgentContextRepository


class DBAgentContextRepository(AgentContextRepository):

    def __init__(self, db_session: AsyncSession):
        self.db_session = db_session



    async def append_many(self, messages: list[AgentMessage]) -> list[AgentMessage]:
        records = [
            AgentMessageModel.from_domain(message)
            for message in messages
        ]

        self.db_session.add_all(records)
        await self.db_session.flush()
        return [record.to_domain() for record in records]


    async def list_active_by_session(self, session_id: str, agent_name: str) -> list[AgentMessage]:

        stmt = (
            select(AgentMessageModel)
            .where(
                AgentMessageModel.session_id == session_id,
                AgentMessageModel.agent_name == agent_name,
                AgentMessageModel.compacted_at.is_(None), # compacted_at为空，即尚未进入摘要
                AgentMessageModel.excluded_at.is_(None), # excluded_at为空，即没有被排除
            )
            .order_by(AgentMessageModel.sequence.asc())
        )

        result = await self.db_session.execute(stmt)
        return [record.to_domain() for record in result.scalars()]


    async def list_active_by_run(self, run_id: str, agent_name: str) -> list[AgentMessage]:
        stmt = (
            select(AgentMessageModel)
            .where(
                AgentMessageModel.run_id == run_id,
                AgentMessageModel.agent_name == agent_name,
                AgentMessageModel.compacted_at.is_(None),
                AgentMessageModel.excluded_at.is_(None),
            )
            .order_by(AgentMessageModel.sequence.asc())
        )
        result = await self.db_session.execute(stmt)

        return [
            record.to_domain()
            for record in result.scalars().all()
        ]


    async def list_by_run(self, run_id: str, agent_name: str) -> list[AgentMessage]:

        stmt = (
            select(AgentMessageModel)
            .where(
                AgentMessageModel.run_id == run_id,
                AgentMessageModel.agent_name == agent_name,
                AgentMessageModel.excluded_at.is_(None),
            )
            .order_by(AgentMessageModel.sequence.asc(),)
        )
        result = await self.db_session.execute(stmt)

        return [
            record.to_domain()
            for record in result.scalars().all()
        ]


    async def get_summary(self, session_id: str, agent_name: str) -> AgentContextSummary | None:

        """获取指定Agent当前的上下文摘要。

               数据库通过以下唯一约束保证每个Agent只有一条当前摘要：

                   UNIQUE(session_id, agent_name)

               Args:
                   session_id:
                       当前的session

                   agent_name:
                       Agent名称。

               Returns:
                   找到时返回AgentContextSummary，否则返回None。
        """
        stmt = (
            select(AgentContextSummaryModel)
            .where(
                AgentContextSummaryModel.session_id == session_id,
                AgentContextSummaryModel.agent_name == agent_name,
            )
            .limit(1)
        )

        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            return None

        return (
            record.to_domain()
            if record is not None
            else None
        )


    async def save_summary(
            self,
            summary: AgentContextSummary,
            expected_version: int | None,
    ) -> bool:
        """使用PostgreSQL Upsert和版本条件原子保存摘要。

        首次新增时，唯一键冲突返回False；更新时，只有数据库中的version
        仍等于expected_version才允许覆盖。这样即使多个Worker同时生成摘要，
        较晚完成的旧版本也无法覆盖已经提交的新版本。
        """

        insert_stmt = (
            insert(AgentContextSummaryModel)
            .values(
                id=summary.id,
                session_id=summary.session_id,
                last_run_id=summary.last_run_id,
                agent_name=summary.agent_name,
                summary=summary.summary,
                summarized_through_sequence=(
                    summary.summarized_through_sequence
                ),
                version=summary.version,
                token_count=summary.token_count,
                created_at=summary.created_at,
                updated_at=summary.updated_at,
            )
        )

        if expected_version is None:
            # 首次创建时发生唯一键冲突，表示另一个任务已抢先写入。
            save_statement = insert_stmt.on_conflict_do_nothing(
                constraint="uq_agent_context_summary_session_agent",
            )
        else:
            # 把版本检查与更新合并成一条原子SQL，消除检查后写入竞态。
            save_statement = insert_stmt.on_conflict_do_update(
                constraint="uq_agent_context_summary_session_agent",
                set_={
                    "last_run_id": insert_stmt.excluded.last_run_id,
                    "summary": insert_stmt.excluded.summary,
                    "summarized_through_sequence": (
                        insert_stmt.excluded.summarized_through_sequence
                    ),
                    "version": insert_stmt.excluded.version,
                    "token_count": insert_stmt.excluded.token_count,
                    "updated_at": insert_stmt.excluded.updated_at,
                },
                where=(
                    AgentContextSummaryModel.version
                    == expected_version
                ),
            )

        result = await self.db_session.execute(
            save_statement.returning(AgentContextSummaryModel.id)
        )
        return result.scalar_one_or_none() is not None


    async def mark_compacted_through(
            self,
            session_id: str,
            agent_name: str,
            sequence: int,
    ) -> None:
        """将指定sequence及其之前的消息标记为已压缩。

                例如当前消息序列为：

                    101, 102, 103, 104, 105

                执行：

                    mark_compacted_through(
                        session_id=session_id,
                        agent_name="planner",
                        sequence=103,
                    )

                将会标记：

                    101, 102, 103

                不会标记：

                    104, 105

                下一次调用list_active()时，只会得到104和105。

                更新时还会限制excluded_at为空。已经被排除的消息不需要再标记为
                已压缩，因为它们本来就不会进入上下文和摘要。

                Args:
                    session_id:
                        当前的session_id。

                    agent_name:
                        Agent名称。

                    sequence:
                        本次摘要已经覆盖到的最后一条消息序号。
        """

        compacted_at = datetime.now(timezone.utc)

        stmt = (
            update(AgentMessageModel)
            .where(
                AgentMessageModel.session_id == session_id,
                AgentMessageModel.agent_name == agent_name,
                AgentMessageModel.sequence <= sequence,
                AgentMessageModel.compacted_at.is_(None),
                AgentMessageModel.excluded_at.is_(None),
            )
            .values(
                compacted_at=compacted_at,
            )
        )

        await self.db_session.execute(stmt)


    async def mark_excluded(self, message_ids: list[str]) -> None:
        if not message_ids:
            return

        stmt = (
            update(AgentMessageModel)
            .where(
                AgentMessageModel.id.in_(message_ids),
                AgentMessageModel.excluded_at.is_(None),
            )
            .values(
                excluded_at=datetime.now(timezone.utc),
            )
        )

        await self.db_session.execute(stmt)
