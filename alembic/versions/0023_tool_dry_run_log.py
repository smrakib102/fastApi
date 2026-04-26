"""v4 governance: tool_dry_run_log table

Revision ID: 0023_tool_dry_run_log
Revises: 0022_tool_confirmations
Create Date: 2026-04-26

Dry-run lane audit log. Records simulated tool results when DRY_RUN is on.
Used later by the Validation Kernel and admin dashboards.

Additive only.
"""

from alembic import op
import sqlalchemy as sa


revision = "0023_tool_dry_run_log"
down_revision = "0022_tool_confirmations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_dry_run_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("args_redacted", sa.Text(), nullable=True),
        sa.Column("simulated_result", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="ok"),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("meta_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_tool_dry_run_user_created", "tool_dry_run_log", ["user_id", "created_at"])
    op.create_index("ix_tool_dry_run_tool_created", "tool_dry_run_log", ["tool_name", "created_at"])
    op.create_index("ix_tool_dry_run_run", "tool_dry_run_log", ["run_id", "step_index"])


def downgrade() -> None:
    op.drop_index("ix_tool_dry_run_run", table_name="tool_dry_run_log")
    op.drop_index("ix_tool_dry_run_tool_created", table_name="tool_dry_run_log")
    op.drop_index("ix_tool_dry_run_user_created", table_name="tool_dry_run_log")
    op.drop_table("tool_dry_run_log")
