"""hardening: performance indexes (S6)

Revision ID: 0015_hardening_indexes
Revises: 0014_unified_chat_memory
Create Date: 2026-04-25

Adds indexes flagged by the production hardening audit. All additive;
no existing indexes are touched. Safe to apply online.
"""

from alembic import op


revision = "0015_hardening_indexes"
down_revision = "0014_unified_chat_memory"
branch_labels = None
depends_on = None


_NEW_INDEXES = [
    ("ix_agent_runs_user_created", "agent_runs", ["user_id", "created_at"]),
    ("ix_agent_run_steps_run_step", "agent_run_steps", ["run_id", "step_index"]),
    ("ix_tool_requests_user_status", "tool_requests", ["user_id", "status"]),
    ("ix_google_accounts_user_id", "google_accounts", ["user_id"]),
    ("ix_conversations_user_lastmsg", "conversations", ["user_id", "last_message_at"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = None
    try:
        from sqlalchemy import inspect

        insp = inspect(bind)
    except Exception:
        insp = None

    for name, table, cols in _NEW_INDEXES:
        existing: list[str] = []
        if insp is not None:
            try:
                existing = [ix["name"] for ix in insp.get_indexes(table)]
            except Exception:
                existing = []
        if name in existing:
            continue
        op.create_index(name, table, cols)


def downgrade() -> None:
    for name, table, _ in reversed(_NEW_INDEXES):
        try:
            op.drop_index(name, table_name=table)
        except Exception:
            # tolerate already-dropped indexes
            pass
