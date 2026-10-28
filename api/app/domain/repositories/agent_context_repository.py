from typing import Protocol

from app.domain.models.agent_context_summary import AgentContextSummary
from app.domain.models.agent_message import AgentMessage


class AgentContextRepository(Protocol):

    async def append_many(self, messages: list[AgentMessage]) -> list[AgentMessage]:
        ...

    async def list_active_by_session(self, session_id: str, agent_name: str) -> list[AgentMessage]:
        """查询Session内所有尚未压缩、尚未排除的消息。"""
        ...


    async def list_active_by_run(self, run_id: str, agent_name: str) -> list[AgentMessage]:
        """读取当前Run尚未压缩、尚未排除的消息。"""
        ...

    async def list_by_run(self, run_id: str, agent_name: str) -> list[AgentMessage]:
        """查询指定Run的消息，用于恢复和调试。"""
        ...


    async def get_summary(
            self,
            session_id: str,
            agent_name: str,
    ) -> AgentContextSummary | None: ...

    async def save_summary(
            self,
            summary: AgentContextSummary,
            expected_version: int | None,
    ) -> bool:
        """使用乐观锁保存摘要，成功返回True，版本冲突返回False。"""
        ...

    async def mark_compacted_through(
            self,
            session_id: str,
            agent_name: str,
            sequence: int,
    ) -> None: ...

    async def mark_excluded(self, message_ids: list[str]) -> None:
        """将不应该再进入上下文的消息标记为排除。"""
        ...
