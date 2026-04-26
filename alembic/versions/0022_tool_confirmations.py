"""v4 governance: tool_confirmations table

Revision ID: 0022_tool_confirmations
Revises: 0021_tool_risk_profiles
Create Date: 2026-04-26

HITL confirmation queue. Stores pending tool approvals and their
resolutions. Used later by the Safety Kernel and admin endpoints.

Additive only.
"""

from alembic import op
import sqlalchemy as sa


revision = "0022_tool_confirmations"
down_revision = "0021_tool_risk_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_confirmations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("args_redacted", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=120), nullable=True),
        sa.UniqueConstraint("token", name="uq_tool_confirmations_token"),
    )
    op.create_index("ix_tool_confirmations_user_status", "tool_confirmations", ["user_id", "status"])
    op.create_index("ix_tool_confirmations_tool", "tool_confirmations", ["tool_name", "requested_at"])
    op.create_index("ix_tool_confirmations_run", "tool_confirmations", ["run_id", "step_index"])


def downgrade() -> None:
    op.drop_index("ix_tool_confirmations_run", table_name="tool_confirmations")
    op.drop_index("ix_tool_confirmations_tool", table_name="tool_confirmations")
    op.drop_index("ix_tool_confirmations_user_status", table_name="tool_confirmations")
    op.drop_table("tool_confirmations")
