"""v4 governance: tool_risk_profiles table + seed

Revision ID: 0021_tool_risk_profiles
Revises: 0020_tool_grants
Create Date: 2026-04-26

Risk registry for tools. Used later by the Safety Kernel and HITL gate to
classify tool invocations. Seeded with conservative defaults for the
current built-in tool set.

Additive only.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


revision = "0021_tool_risk_profiles"
down_revision = "0020_tool_grants"
branch_labels = None
depends_on = None


_SEED_PROFILES = [
    # Read-only tools
    ("gmail.profile", "low", False, False, "Read-only Gmail profile"),
    ("gmail.list_messages", "low", False, False, "Read-only Gmail listing"),
    ("gmail.list_drafts", "low", False, False, "Read-only Gmail drafts list"),
    ("calendar.list", "low", False, False, "Read-only Calendar listing"),
    ("telegram.group_summary", "low", False, False, "Read-only Telegram summary"),
    # Drafting / request creation
    ("gmail.draft", "medium", False, False, "Creates a Gmail draft (no send)"),
    ("gmail.send_request", "high", True, False, "Creates approval to send Gmail"),
    ("calendar.create_request", "high", True, False, "Creates approval to add event"),
    # Sending / side-effecting
    ("gmail.send", "critical", True, False, "Sends email (side-effecting)"),
    # Future universal HTTP tool (conservative default)
    ("api.request", "high", True, True, "External HTTP request (future tool)"),
]


def upgrade() -> None:
    op.create_table(
        "tool_risk_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        # low | medium | high | critical
        sa.Column("risk_tier", sa.String(length=16), nullable=False, server_default="medium"),
        sa.Column("requires_hitl", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("requires_dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False, server_default="default"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tool_name", name="uq_tool_risk_profiles_tool"),
    )
    op.create_index("ix_tool_risk_profiles_tier", "tool_risk_profiles", ["risk_tier"])

    seed_table = table(
        "tool_risk_profiles",
        column("tool_name", sa.String),
        column("risk_tier", sa.String),
        column("requires_hitl", sa.Boolean),
        column("requires_dry_run", sa.Boolean),
        column("description", sa.String),
        column("source", sa.String),
    )
    op.bulk_insert(
        seed_table,
        [
            {
                "tool_name": name,
                "risk_tier": tier,
                "requires_hitl": hitl,
                "requires_dry_run": dry_run,
                "description": desc,
                "source": "default",
            }
            for (name, tier, hitl, dry_run, desc) in _SEED_PROFILES
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_risk_profiles_tier", table_name="tool_risk_profiles")
    op.drop_table("tool_risk_profiles")
