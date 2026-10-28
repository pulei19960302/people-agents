"""update last_run_id foreignKey

Revision ID: b381a9617b77
Revises: 6b01bca10c2d
Create Date: 2026-10-27 15:44:06.454585

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b381a9617b77'
down_revision: Union[str, Sequence[str], None] = '6b01bca10c2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(
        "agent_context_summaries_last_run_id_fkey",
        "agent_context_summaries",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "agent_context_summaries_last_run_id_fkey",
        "agent_context_summaries",
        "session_runs",
        ["last_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "agent_context_summaries_last_run_id_fkey",
        "agent_context_summaries",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "agent_context_summaries_last_run_id_fkey",
        "agent_context_summaries",
        "session_runs",
        ["last_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
