"""Add calendar.update_request to tool risk profiles

Revision ID: 0025_tool_risk_profiles_calendar_update
Revises: 0024_tool_confirmations_meta_json
Create Date: 2026-04-27
"""

from alembic import op
import sqlalchemy as sa


revision = "0025_tool_risk_profiles_calendar_update"
down_revision = "0024_tool_confirmations_meta_json"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO tool_risk_profiles (
                tool_name,
                risk_tier,
                requires_hitl,
                requires_dry_run,
                description,
                source
            )
            VALUES (
                'calendar.update_request',
                'high',
                true,
                false,
                'Creates approval to update calendar event',
                'default'
            )
            ON CONFLICT (tool_name) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM tool_risk_profiles WHERE tool_name = 'calendar.update_request'"
        )
    )
