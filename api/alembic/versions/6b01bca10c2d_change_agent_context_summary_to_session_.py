"""change agent context summary to session scope

Revision ID: 6b01bca10c2d
Revises: a12804ec94bf
Create Date: 2026-10-27 15:22:10.578142

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6b01bca10c2d'
down_revision: Union[str, Sequence[str], None] = 'a12804ec94bf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "agent_context_summaries",
        sa.Column("last_run_id", sa.String(length=255), nullable=True),
    )

    # 在删除旧run_id之前保留其值，避免已有摘要丢失来源信息。
    op.execute(
        """
        UPDATE agent_context_summaries
        SET last_run_id = run_id
        """
    )

    op.drop_constraint(
        "uq_agent_context_summary_run_agent",
        "agent_context_summaries",
        type_="unique",
    )
    op.drop_constraint(
        "agent_context_summaries_run_id_fkey",
        "agent_context_summaries",
        type_="foreignkey",
    )
    op.drop_column("agent_context_summaries", "run_id")

    op.create_foreign_key(
        "agent_context_summaries_last_run_id_fkey",
        "agent_context_summaries",
        "session_runs",
        ["last_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_agent_context_summary_session_agent",
        "agent_context_summaries",
        ["session_id", "agent_name"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "agent_context_summaries",
        sa.Column("run_id", sa.String(length=255), nullable=True),
    )
    op.execute(
        """
        UPDATE agent_context_summaries
        SET run_id = last_run_id
        """
    )

    # last_run_id可能因对应Run被删除而被SET NULL。此时旧结构要求run_id
    # 非空，无法无损降级，因此明确终止，而不是静默删除摘要数据。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM agent_context_summaries
                WHERE run_id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade: agent context summary has no source run';
            END IF;
        END
        $$
        """
    )

    op.drop_constraint(
        "uq_agent_context_summary_session_agent",
        "agent_context_summaries",
        type_="unique",
    )
    op.drop_constraint(
        "agent_context_summaries_last_run_id_fkey",
        "agent_context_summaries",
        type_="foreignkey",
    )
    op.drop_column("agent_context_summaries", "last_run_id")

    op.alter_column(
        "agent_context_summaries",
        "run_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.create_foreign_key(
        "agent_context_summaries_run_id_fkey",
        "agent_context_summaries",
        "session_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_agent_context_summary_run_agent",
        "agent_context_summaries",
        ["run_id", "agent_name"],
    )
