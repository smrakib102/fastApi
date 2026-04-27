"""v4 governance: tool_confirmations meta_json payload

Revision ID: 0024_tool_confirmations_meta_json
Revises: 0023_tool_dry_run_log
Create Date: 2026-04-26

Stores full execution payload for HITL resume.
"""

from alembic import op
import sqlalchemy as sa


revision = "0024_tool_confirmations_meta_json"
down_revision = "0023_tool_dry_run_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tool_confirmations", sa.Column("meta_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tool_confirmations", "meta_json")
