"""v4 governance: tool_grants table

Revision ID: 0020_tool_grants
Revises: 0019_blocked_domains
Create Date: 2026-04-26

Permission v2 foundation. Grants can be allow/deny with optional scope JSON
(domain/path/method constraints), expiry, and revocation metadata.

Additive only.
"""

from alembic import op
import sqlalchemy as sa


revision = "0020_tool_grants"
down_revision = "0019_blocked_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        # allow | deny
        sa.Column("permission", sa.String(length=16), nullable=False, server_default="allow"),
        # JSON scope (serialized). Example:
        #   {"domains": ["api.example.com"], "methods": ["GET"], "paths": ["/v1/*"]}
        sa.Column("scope_json", sa.Text(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False, server_default="admin"),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("revoked_by", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tool_grants_user", "tool_grants", ["user_id", "created_at"])
    op.create_index("ix_tool_grants_tool", "tool_grants", ["tool_name", "created_at"])
    op.create_index("ix_tool_grants_agent", "tool_grants", ["agent_id", "created_at"])
    op.create_index("ix_tool_grants_active", "tool_grants", ["revoked_at", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_tool_grants_active", table_name="tool_grants")
    op.drop_index("ix_tool_grants_agent", table_name="tool_grants")
    op.drop_index("ix_tool_grants_tool", table_name="tool_grants")
    op.drop_index("ix_tool_grants_user", table_name="tool_grants")
    op.drop_table("tool_grants")
