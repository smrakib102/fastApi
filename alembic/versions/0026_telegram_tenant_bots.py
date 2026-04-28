"""Per-user Telegram bots

Revision ID: 0026_telegram_tenant_bots
Revises: 0025_tool_risk_profiles_calendar_update
Create Date: 2026-04-28
"""

from alembic import op
import sqlalchemy as sa


revision = "0026_telegram_tenant_bots"
down_revision = "0025_tool_risk_profiles_calendar_update"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_bots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("bot_token", sa.String(length=512), nullable=True),
        sa.Column("bot_username", sa.String(length=120), nullable=True),
        sa.Column("bot_id", sa.String(length=64), nullable=True),
        sa.Column("webhook_secret", sa.String(length=120), nullable=True),
        sa.Column("start_token", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("connected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", name="telegram_bots_user_unique"),
        sa.UniqueConstraint("webhook_secret", name="telegram_bots_webhook_secret_unique"),
        sa.UniqueConstraint("start_token", name="telegram_bots_start_token_unique"),
    )

    op.drop_constraint("telegram_links_user_unique", "telegram_links", type_="unique")
    op.create_unique_constraint(
        "telegram_links_user_tg_unique",
        "telegram_links",
        ["user_id", "telegram_user_id"],
    )


def downgrade() -> None:
    op.drop_constraint("telegram_links_user_tg_unique", "telegram_links", type_="unique")
    op.create_unique_constraint(
        "telegram_links_user_unique",
        "telegram_links",
        ["telegram_user_id"],
    )
    op.drop_table("telegram_bots")
