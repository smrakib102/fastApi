"""OAuth credential vault tables

Revision ID: 0027_oauth_credentials_vault
Revises: 0026_telegram_tenant_bots
Create Date: 2026-04-29
"""

from alembic import op
import sqlalchemy as sa


revision = "0027_oauth_credentials_vault"
down_revision = "0026_telegram_tenant_bots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=255), nullable=False),
        sa.Column("account_email", sa.String(length=255), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.String(length=32), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "provider_account_id",
            name="uq_oauth_credentials_identity",
        ),
    )
    op.create_index(
        "ix_oauth_credentials_user_provider",
        "oauth_credentials",
        ["user_id", "provider"],
    )
    op.create_index(
        "ix_oauth_credentials_provider_account",
        "oauth_credentials",
        ["provider_account_id"],
    )

    op.create_table(
        "agent_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.Integer(), nullable=False),
        sa.Column("required_scopes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "agent_id",
            "credential_id",
            name="uq_agent_credentials_agent_credential",
        ),
    )
    op.create_index(
        "ix_agent_credentials_agent",
        "agent_credentials",
        ["agent_id"],
    )
    op.create_index(
        "ix_agent_credentials_credential",
        "agent_credentials",
        ["credential_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_credentials_credential", table_name="agent_credentials")
    op.drop_index("ix_agent_credentials_agent", table_name="agent_credentials")
    op.drop_table("agent_credentials")

    op.drop_index("ix_oauth_credentials_provider_account", table_name="oauth_credentials")
    op.drop_index("ix_oauth_credentials_user_provider", table_name="oauth_credentials")
    op.drop_table("oauth_credentials")
