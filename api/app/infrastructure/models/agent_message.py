from sqlalchemy import String, ForeignKey, BigInteger, Identity, Integer, DateTime, text, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from typing import Any, Optional
from datetime import datetime
from app.domain.models.agent_message import AgentMessage
from .base import Base

class AgentMessageModel(Base):

    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)

    session_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )

    run_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("session_runs.id", ondelete="CASCADE"),
        nullable=False,
    )

    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)

    sequence: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    message: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )

    tool_call_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    compacted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    excluded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at:Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


    __table_args__ = (
        Index(
            "ix_agent_messages_run_agent_sequence",
            "run_id",
            "agent_name",
            "sequence",
        ),

        Index(
            "ix_agent_messages_session_created_at",
            "session_id",
            "created_at",
        )
    )

    @classmethod
    def from_domain(cls, item: AgentMessage) -> "AgentMessageModel":
        return cls(**item.model_dump(mode="python", exclude={"sequence"}))


    def to_domain(self) -> AgentMessage:
        return AgentMessage.model_validate(self, from_attributes=True)