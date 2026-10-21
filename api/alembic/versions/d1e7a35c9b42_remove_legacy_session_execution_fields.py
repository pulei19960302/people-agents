"""remove legacy session execution fields

Revision ID: d1e7a35c9b42
Revises: c841f219add4
Create Date: 2026-10-21 20:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d1e7a35c9b42"
down_revision: Union[str, Sequence[str], None] = "c841f219add4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Session只保留会话数据，任务执行状态由session_runs负责。"""
    op.drop_column("sessions", "task_id")
    op.drop_column("sessions", "status")


def downgrade() -> None:
    """恢复旧版Session执行字段。"""
    op.add_column(
        "sessions",
        sa.Column(
            "status",
            sa.String(length=255),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "task_id",
            sa.String(length=255),
            nullable=True,
        ),
    )
