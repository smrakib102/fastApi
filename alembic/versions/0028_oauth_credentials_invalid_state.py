"""Add invalid-state tracking to oauth credentials

Revision ID: 0028_oauth_credentials_invalid_state
Revises: 0027_oauth_credentials_vault
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0028_oauth_credentials_invalid_state"
down_revision = "0027_oauth_credentials_vault"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "oauth_credentials",
        sa.Column("invalid_state", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "oauth_credentials",
        sa.Column("invalid_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "oauth_credentials",
        sa.Column("invalid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("oauth_credentials", "invalid_state", server_default=None)


def downgrade() -> None:
    op.drop_column("oauth_credentials", "invalid_at")
    op.drop_column("oauth_credentials", "invalid_reason")
    op.drop_column("oauth_credentials", "invalid_state")
