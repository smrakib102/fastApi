"""v4 governance: feature_flags table

Revision ID: 0016_feature_flags
Revises: 0015_hardening_indexes
Create Date: 2026-04-26

Foundation step for the v4 production-governance rollout. Purely additive:
introduces a single `feature_flags` table that the new feature_flags service
will read at runtime. Nothing in the existing codebase references this table
yet, so applying this migration is a no-op behaviourally.

Resolution order at runtime (implemented in app/services/feature_flags.py):
    1. .env value (if explicitly set) — emergency authority
    2. DB row in this table — admin runtime control
    3. Settings default — safe fallback (always OFF for v4 flags)

Specifically for SAFE_MODE_ENABLED and STRICT_MODE_ENABLED, the .env value
ALWAYS wins over DB so operators retain emergency shutdown via a restart.
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_feature_flags"
down_revision = "0015_hardening_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        sa.Column("key", sa.String(length=100), primary_key=True),
        # JSON-encoded value so we can store bool / str ("off|shadow|enforce") /
        # int / small object without schema changes.
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("feature_flags")
