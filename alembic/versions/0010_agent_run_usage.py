"""add run usage tracking

Revision ID: 0010_agent_run_usage
Revises: 0009_agent_run_async_fields
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_agent_run_usage"
down_revision = "0009_agent_run_async_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("summary_memory", sa.Text(), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_runs",
        sa.Column("total_cost_usd", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_run_steps",
        sa.Column("tokens_used", sa.Integer(), nullable=True),
    )
    op.add_column(
        "agent_run_steps",
        sa.Column("cost_usd", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_run_steps", "cost_usd")
    op.drop_column("agent_run_steps", "tokens_used")
    op.drop_column("agent_runs", "total_cost_usd")
    op.drop_column("agent_runs", "total_tokens")
    op.drop_column("agent_runs", "summary_memory")
