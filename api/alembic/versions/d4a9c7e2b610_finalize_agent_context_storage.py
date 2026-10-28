"""finalize agent context storage

Revision ID: d4a9c7e2b610
Revises: b381a9617b77
Create Date: 2026-10-27 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "d4a9c7e2b610"
down_revision: Union[str, Sequence[str], None] = "b381a9617b77"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """切换到独立上下文表，并优化活跃消息查询。"""
    op.drop_index(
        "ix_agent_messages_session_created_at",
        table_name="agent_messages",
    )
    op.create_index(
        "ix_agent_messages_session_agent_active_sequence",
        "agent_messages",
        ["session_id", "agent_name", "sequence"],
        unique=False,
        postgresql_where=sa.text(
            "compacted_at IS NULL AND excluded_at IS NULL"
        ),
    )

    # 新上下文已经完全由agent_messages和agent_context_summaries承载。
    op.drop_column("sessions", "memories")


def downgrade() -> None:
    """恢复旧JSONB字段和旧索引。"""
    op.add_column(
        "sessions",
        sa.Column(
            "memories",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    op.drop_index(
        "ix_agent_messages_session_agent_active_sequence",
        table_name="agent_messages",
    )
    op.create_index(
        "ix_agent_messages_session_created_at",
        "agent_messages",
        ["session_id", "created_at"],
        unique=False,
    )
