
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.session_run import SessionRun
from app.infrastructure.models.base import Base

class SessionRunModel(Base):
     """会话内一轮逻辑任务的ORM模型。"""

     __tablename__ = "session_runs"

     __table_args__ = (
         PrimaryKeyConstraint("id", name="pk_session_runs_id"),
         Index(
             "ix_session_runs_session_created_at_id",
             "session_id",
             "created_at",
             "id",
         ),
         Index(
             "ix_session_runs_status_heartbeat_at",
             "status",
             "heartbeat_at",
         ),
         Index(
             "uq_session_runs_active_task_id",
             "active_task_id",
             unique=True,
             postgresql_where=text("active_task_id IS NOT NULL"),
         ),
         Index(
             "uq_session_runs_one_active_per_session",
             "session_id",
             unique=True,
             postgresql_where=text(
                 "status IN ('pending', 'running', 'waiting')"
             ),
         ),
     )


     id: Mapped[str] = mapped_column(
         String(255),
         default=lambda: str(uuid.uuid4()),
         nullable=False,
     )

     session_id: Mapped[str] = mapped_column(
         String(255),
         ForeignKey(
             "sessions.id",
             name="fk_session_runs_session_id",
             ondelete="CASCADE",
             onupdate="CASCADE",
         ),
         nullable=False,
     )

     status: Mapped[str] = mapped_column(
         String(32),
         nullable=False,
         server_default=text("'pending'"),
     )

     active_task_id: Mapped[str | None] = mapped_column(
         String(255),
         nullable=True,
     )

     attempt_count: Mapped[int] = mapped_column(
         Integer,
         nullable=False,
         server_default=text("0"),
     )

     current_step_id: Mapped[str | None] = mapped_column(
         String(255),
         nullable=True,
     )

     checkpoint: Mapped[dict[str, Any]] = mapped_column(
         JSONB,
         nullable=False,
         server_default=text("'{}'::jsonb"),
     )

     error: Mapped[str | None] = mapped_column(
         Text,
         nullable=True,
     )

     started_at: Mapped[datetime | None] = mapped_column(
         DateTime(timezone=True),
         nullable=True,
     )

     heartbeat_at: Mapped[datetime | None] = mapped_column(
         DateTime(timezone=True),
         nullable=True,
     )

     finished_at: Mapped[datetime | None] = mapped_column(
         DateTime(timezone=True),
         nullable=True,
     )

     created_at: Mapped[datetime] = mapped_column(
         DateTime(timezone=True),
         nullable=False,
         server_default=text("CURRENT_TIMESTAMP"),
     )

     updated_at: Mapped[datetime] = mapped_column(
         DateTime(timezone=True),
         nullable=False,
         server_default=text("CURRENT_TIMESTAMP")
     )


     @classmethod
     def from_domain(cls, session_run: SessionRun) -> "SessionRunModel":
        data = session_run.model_dump(mode="python", exclude={"checkpoint"})
        checkpoint = session_run.model_dump(
            mode="json",
            include={"checkpoint"},
        )["checkpoint"]

        return cls(**data, checkpoint=checkpoint)


     def to_domain(self) -> SessionRun:
         return SessionRun.model_validate(self, from_attributes=True)


     def update_from_domain(self, run: SessionRun) -> None:
         data = run.model_dump(mode="python", exclude={"checkpoint"})
         data["checkpoint"] = run.model_dump(
             mode="json",
             include={"checkpoint"},
         )["checkpoint"]

         for field, value in data.items():
             setattr(self, field, value)