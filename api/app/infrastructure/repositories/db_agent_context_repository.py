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


    async def save_summary(self, summary: AgentContextSummary) -> None:
        """新增或更新Agent上下文摘要。
               使用PostgreSQL的INSERT ON CONFLICT实现原子Upsert：
               - 如果(session_id, agent_name)不存在，则新增摘要；
               - 如果(session_id, agent_name)已经存在，则更新原来的摘要。
               使用数据库Upsert能够避免以下并发问题：
               1. 请求A查询发现摘要不存在；
               2. 请求B也查询发现摘要不存在；
               3. A和B同时尝试INSERT；
               4. 其中一个请求违反唯一约束。
               冲突更新时保留原记录的：

               - id；
               - created_at。

               更新以下字段：

               - session_id；
               - summary；
               - summarized_through_sequence；
               - version；
               - token_count；
               - updated_at。

               Args:
                   summary:
                       需要新增或更新的摘要领域模型。
               """

        now = datetime.now(timezone.utc)
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

        # 使用on_conflict_do_update 将insert 变成upsert。违反约束就行
        upsert_statement = insert_stmt.on_conflict_do_update(
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
            }
        )

        await self.db_session.execute(upsert_statement)


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