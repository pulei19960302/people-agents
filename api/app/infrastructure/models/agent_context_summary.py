from sqlalchemy import String, ForeignKey, BigInteger, Integer, DateTime, text, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.domain.models.agent_context_summary import AgentContextSummary
from datetime import datetime
from typing import Optional
from .base import Base


class AgentContextSummaryModel(Base):

    __tablename__ = "agent_context_summaries"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)

    session_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("sessions.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )

    # 仅记录最后一次参与摘要的Run。
    # 删除某个Run时不能删除Session摘要，所以使用SET NULL。
    last_run_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        ForeignKey("session_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    agent_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
    )

    summarized_through_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=datetime.now,
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "agent_name",
            name="uq_agent_context_summary_session_agent",
        ),
    )

    @classmethod
    def from_domain(
            cls,
            item: AgentContextSummary,
    ) -> "AgentContextSummaryModel":
        return cls(**item.model_dump(mode="python"))

    def to_domain(self) -> AgentContextSummary:
        return AgentContextSummary.model_validate(
            self,
            from_attributes=True,
        )
