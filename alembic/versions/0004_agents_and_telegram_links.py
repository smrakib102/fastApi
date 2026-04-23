"""agents category/status and telegram links

Revision ID: 0004_agents_and_telegram_links
Revises: 0003_users_and_scoping
Create Date: 2026-04-23

"""
from alembic import op
import sqlalchemy as sa

revision = "0004_agents_and_telegram_links"
down_revision = "0003_users_and_scoping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("category", sa.String(length=80), nullable=False, server_default="general"),
    )
    op.add_column(
        "agents",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
    )

    op.create_table(
        "telegram_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("telegram_user_id", name="telegram_links_user_unique"),
    )


def downgrade() -> None:
    op.drop_table("telegram_links")
    op.drop_column("agents", "status")
    op.drop_column("agents", "category")
