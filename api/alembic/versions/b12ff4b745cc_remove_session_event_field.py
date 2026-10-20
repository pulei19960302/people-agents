"""remove session event field

Revision ID: b12ff4b745cc
Revises: 72af2cc320fa
Create Date: 2026-10-15 15:20:33.498783

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b12ff4b745cc'
down_revision: Union[str, Sequence[str], None] = '72af2cc320fa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # id 和 event_id 在领域模型中仍然是字符串，本次迁移不改变字段类型。
    op.drop_index(
        "ix_session_events_session_id_id",
        table_name="session_events",
    )
    op.drop_index(
        "uq_session_events_session_event_id",
        table_name="session_events",
    )
    op.create_unique_constraint(
        "uq_session_events_event_id",
        "session_events",
        ["event_id"],
    )
    op.create_index(
        "ix_session_events_session_created_at_id",
        "session_events",
        ["session_id", "created_at", "id"],
        unique=False,
    )

    # 保留删除会话时级联删除事件，并补充 session id 更新时的级联行为。
    op.drop_constraint(
        "fk_session_events_session_id",
        "session_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_session_events_session_id",
        "session_events",
        "sessions",
        ["session_id"],
        ["id"],
        onupdate="CASCADE",
        ondelete="CASCADE",
    )

    # 事件已迁移到 session_events 独立表，删除 sessions 中的旧 JSONB 字段。
    op.drop_column("sessions", "events")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "sessions",
        sa.Column(
            "events",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )

    op.drop_constraint(
        "fk_session_events_session_id",
        "session_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_session_events_session_id",
        "session_events",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_index(
        "ix_session_events_session_created_at_id",
        table_name="session_events",
    )
    op.drop_constraint(
        "uq_session_events_event_id",
        "session_events",
        type_="unique",
    )
    op.create_index(
        "uq_session_events_session_event_id",
        "session_events",
        ["session_id", "event_id"],
        unique=True,
    )
    op.create_index(
        "ix_session_events_session_id_id",
        "session_events",
        ["session_id", sa.literal_column("id DESC")],
        unique=False,
    )
