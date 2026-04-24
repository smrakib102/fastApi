"""agent run async fields

Revision ID: 0009_agent_run_async_fields
Revises: 0008_agent_run_step_fields
Create Date: 2026-04-24

"""
from alembic import op
import sqlalchemy as sa

revision = "0009_agent_run_async_fields"
down_revision = "0008_agent_run_step_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_runs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "finished_at")
    op.drop_column("agent_runs", "started_at")
    op.drop_column("agent_runs", "error_message")
