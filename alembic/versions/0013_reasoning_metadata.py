"""reasoning metadata for run steps

Revision ID: 0013_reasoning_metadata
Revises: 0012_intelligence_analytics
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0013_reasoning_metadata"
down_revision = "0012_intelligence_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_run_steps", sa.Column("reasoning_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_run_steps", "reasoning_json")
