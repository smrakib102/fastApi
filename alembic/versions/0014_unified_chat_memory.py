"""unified chat memory: conversations + chat_messages

Revision ID: 0014_unified_chat_memory
Revises: 0013_reasoning_metadata
Create Date: 2026-04-25

Phase 1 of the unified chat refactor. Additive only: introduces two new
tables used by the upcoming ChatService / MemoryService. No existing
tables or columns are modified, so this migration is safe to apply on
production without functional changes (nothing reads/writes these tables
yet).
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_unified_chat_memory"
down_revision = "0013_reasoning_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("external_ref", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("meta_json", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_channel", "conversations", ["channel"])
    op.create_index("ix_conversations_agent_id", "conversations", ["agent_id"])
    op.create_index("ix_conversations_external_ref", "conversations", ["external_ref"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("intent", sa.String(length=64), nullable=True),
        sa.Column("meta_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_chat_messages_conversation_id", "chat_messages", ["conversation_id"])
    op.create_index("ix_chat_messages_user_id", "chat_messages", ["user_id"])
    op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_messages_created_at", table_name="chat_messages")
    op.drop_index("ix_chat_messages_user_id", table_name="chat_messages")
    op.drop_index("ix_chat_messages_conversation_id", table_name="chat_messages")
    op.drop_table("chat_messages")

    op.drop_index("ix_conversations_external_ref", table_name="conversations")
    op.drop_index("ix_conversations_agent_id", table_name="conversations")
    op.drop_index("ix_conversations_channel", table_name="conversations")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_table("conversations")
