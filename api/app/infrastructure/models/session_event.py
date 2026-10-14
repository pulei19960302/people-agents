from datetime import datetime

import uuid

from sqlalchemy import String, ForeignKey, text, DateTime, PrimaryKeyConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from typing import Optional, Any, Dict

from .base import Base
from ...domain.models.session_event import SessionEvent


class SessionEventModel(Base):

    __tablename__ = "session_events"

    id: Mapped[str] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        default=lambda: str(uuid.uuid4())
    )


    event_id: Mapped[str] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        primary_key=False,
        unique=True,
        default=lambda: str(uuid.uuid4())
    )

    session_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey(
            "sessions.id",
            name="fk_session_events_session_id",
            ondelete="CASCADE",
            onupdate="CASCADE"
        ),
        nullable=False,
    )

    run_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=False
    )

    event_type:Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    tool_call_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    payload: Mapped[Dict[str,Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_session_events_id"),

        Index(
            "ix_session_events_session_created_at",
            "session_id",
            "created_at"
        ),
        Index(
            "ix_session_events_session_run_id_id",
            "session_id",
            "run_id",
            "id",
            postgresql_where=text("run_id IS NOT NULL"),
        ),
        Index(
            "ix_session_events_session_tool_call_id",
            "session_id",
            "tool_call_id",
            postgresql_where=text("tool_call_id IS NOT NULL"),
        ),
    )


    @classmethod
    def from_domain(cls, event: SessionEvent) -> "SessionEventModel":
        """从会话事件领域模型创建ORM模型。"""
        # 普通字段使用Python模式。
        # 这样occurred_at和created_at仍然是datetime对象。
        base_data = event.model_dump(
            mode="python",
            exclude={"payload"},
        )

        # JSONB字段使用JSON模式，确保UUID、枚举、Pydantic模型、
        # datetime等复杂内容已经转换为可写入JSON的数据。
        payload = event.model_dump(
            mode="json",
            include={"payload"},
        )["payload"]

        return cls(
            **base_data,
            payload=payload
        )


    def to_domain(self) -> SessionEvent:
        """将ORM模型转换为会话事件领域模型。"""

        return SessionEvent.model_validate(
            self,
            from_attributes=True,
        )
