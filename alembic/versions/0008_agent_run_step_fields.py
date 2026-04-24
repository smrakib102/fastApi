"""agent run step fields

Revision ID: 0008_agent_run_step_fields
Revises: 0007_phases_2_8
Create Date: 2026-04-24

"""
from alembic import op
import sqlalchemy as sa

revision = "0008_agent_run_step_fields"
down_revision = "0007_phases_2_8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_run_steps", sa.Column("step_number", sa.Integer(), nullable=True))
    op.add_column("agent_run_steps", sa.Column("action_type", sa.String(length=32), nullable=True))
    op.add_column("agent_run_steps", sa.Column("thought", sa.Text(), nullable=True))
    op.add_column("agent_run_steps", sa.Column("tool_name", sa.String(length=120), nullable=True))
    op.add_column("agent_run_steps", sa.Column("input_json", sa.Text(), nullable=True))
    op.add_column("agent_run_steps", sa.Column("output_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_run_steps", "output_json")
    op.drop_column("agent_run_steps", "input_json")
    op.drop_column("agent_run_steps", "tool_name")
    op.drop_column("agent_run_steps", "thought")
    op.drop_column("agent_run_steps", "action_type")
    op.drop_column("agent_run_steps", "step_number")
