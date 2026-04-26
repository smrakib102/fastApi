"""v4 governance: tool_call_audit table

Revision ID: 0017_tool_call_audit
Revises: 0016_feature_flags
Create Date: 2026-04-26

Append-only audit log of every tool invocation. Populated later (Step 11+)
by the Validation Kernel / executor. Creating it now so the schema lands
ahead of the code that writes to it.

Additive only. No existing tables are modified. Reads only happen through
admin views built in later steps, so applying this migration is a no-op.
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_tool_call_audit"
down_revision = "0016_feature_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_call_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("step_index", sa.Integer(), nullable=True),
        # Tool identity
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("tool_category", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="builtin"),
        # mode = "live" | "dry_run" | "shadow"
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="live"),
        # Request/response (redacted JSON; PII/secrets stripped before insert)
        sa.Column("args_redacted", sa.Text(), nullable=True),
        sa.Column("result_redacted", sa.Text(), nullable=True),
        # Outcome
        sa.Column("status", sa.String(length=24), nullable=False, server_default="ok"),
        sa.Column("error_class", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        # Kernel decisions (JSON):
        #   {"validation": "pass|warn|block", "intent": "verified|drift|abstain",
        #    "safety": "allow|hitl|deny", "risk_tier": "low|medium|high|critical"}
        sa.Column("kernel_decisions", sa.Text(), nullable=True),
        sa.Column("hitl_required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("hitl_resolution", sa.String(length=24), nullable=True),
        # Cost / rate-limit accounting
        sa.Column("token_cost", sa.Integer(), nullable=True),
        sa.Column("dollar_cost", sa.Numeric(10, 6), nullable=True),
        # Free-form metadata for forward-compat (mcp_server_id, http_request_id, ...)
        sa.Column("meta_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_tool_call_audit_user_created", "tool_call_audit", ["user_id", "created_at"])
    op.create_index("ix_tool_call_audit_run", "tool_call_audit", ["run_id", "step_index"])
    op.create_index("ix_tool_call_audit_tool_created", "tool_call_audit", ["tool_name", "created_at"])
    op.create_index("ix_tool_call_audit_status_created", "tool_call_audit", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_tool_call_audit_status_created", table_name="tool_call_audit")
    op.drop_index("ix_tool_call_audit_tool_created", table_name="tool_call_audit")
    op.drop_index("ix_tool_call_audit_run", table_name="tool_call_audit")
    op.drop_index("ix_tool_call_audit_user_created", table_name="tool_call_audit")
    op.drop_table("tool_call_audit")
