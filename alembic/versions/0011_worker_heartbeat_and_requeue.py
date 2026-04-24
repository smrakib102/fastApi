"""worker heartbeat and run requeue

Revision ID: 0011_worker_heartbeat_and_requeue
Revises: 0010_agent_run_usage
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_worker_heartbeat_and_requeue"
down_revision = "0010_agent_run_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("requeue_count", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("queue_name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_column("agent_runs", "requeue_count")
