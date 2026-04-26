"""v4 governance: tool_credentials_v2 table

Revision ID: 0018_tool_credentials_v2
Revises: 0017_tool_call_audit
Create Date: 2026-04-26

Encrypted-at-rest credential vault for the upcoming universal API tool and
MCP integrations. Populated later by ``app/services/credential_vault.py``
(Step 10).

NOTE: A legacy ``tool_credentials`` table already exists from migration
0005 with a much narrower schema (id, user_id, tool_id, secret). To avoid
schema collision and keep the legacy table untouched, the v4 vault uses
the name ``tool_credentials_v2``. A future migration may consolidate them
once all callers of the legacy table have been retired.

The vault encrypts ``secret_ciphertext`` with the existing
``SECRETS_MASTER_KEY`` Fernet key. Additive only.
"""

from alembic import op
import sqlalchemy as sa


revision = "0018_tool_credentials_v2"
down_revision = "0017_tool_call_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_credentials_v2",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        # Logical credential name as referenced from tool configs / agent specs.
        # e.g. "openai_api", "stripe_live", "github_pat".
        sa.Column("name", sa.String(length=120), nullable=False),
        # Auth scheme: bearer | api_key_header | basic | oauth2 | custom
        sa.Column("scheme", sa.String(length=32), nullable=False),
        # Optional header name override (for api_key_header schemes).
        sa.Column("header_name", sa.String(length=120), nullable=True),
        # Fernet ciphertext of the secret payload (token, key, JSON for oauth2 …).
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        # Scope hint: which hosts this credential is allowed to be sent to.
        # Comma-separated list of host suffixes. Empty = no host restriction
        # (still subject to blocked_domains).
        sa.Column("allowed_hosts", sa.Text(), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "name", name="uq_tool_credentials_v2_user_name"),
    )
    op.create_index("ix_tool_credentials_v2_user", "tool_credentials_v2", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_credentials_v2_user", table_name="tool_credentials_v2")
    op.drop_table("tool_credentials_v2")
