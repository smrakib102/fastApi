"""phases 2-8 schema

Revision ID: 0007_phases_2_8
Revises: 0006_agent_runs
Create Date: 2026-04-24

"""
from alembic import op
import sqlalchemy as sa

revision = "0007_phases_2_8"
down_revision = "0006_agent_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=80), nullable=False, server_default="general"),
        sa.Column("model", sa.String(length=120), nullable=False, server_default="auto"),
        sa.Column("tools", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("fields", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.execute(
        """
        INSERT INTO agent_templates (name, description, category, model, tools, fields)
        VALUES (
            'Telegram Group Summary',
            'Summarize Telegram group messages into daily briefings.',
            'summary',
            'auto',
            '[]',
            '[{"key":"agent_name","label":"Agent name"},{"key":"chat_id","label":"Telegram chat ID"},{"key":"timezone","label":"Timezone (e.g., UTC)"}]'
        );
        """
    )

    op.add_column("agents", sa.Column("template_id", sa.Integer(), nullable=True))
    op.add_column("agents", sa.Column("config", sa.Text(), nullable=True))
    op.create_foreign_key(
        "agents_template_id_fk",
        "agents",
        "agent_templates",
        ["template_id"],
        ["id"],
    )

    op.add_column("approvals", sa.Column("agent_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "approvals_agent_id_fk",
        "approvals",
        "agents",
        ["agent_id"],
        ["id"],
    )

    op.add_column("users", sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    op.create_table(
        "telegram_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("chat_type", sa.String(length=32), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("sender_id", sa.String(length=64), nullable=True),
        sa.Column("sender_name", sa.String(length=200), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_telegram_messages_user_chat", "telegram_messages", ["user_id", "chat_id"])
    op.create_index("ix_telegram_messages_sent_at", "telegram_messages", ["sent_at"])

    op.create_table(
        "summary_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("send_hour", sa.Integer(), nullable=False, server_default="18"),
        sa.Column("send_minute", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("resource_type", sa.String(length=120), nullable=False),
        sa.Column("resource_id", sa.String(length=120), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("summary_schedules")
    op.drop_index("ix_telegram_messages_sent_at", table_name="telegram_messages")
    op.drop_index("ix_telegram_messages_user_chat", table_name="telegram_messages")
    op.drop_table("telegram_messages")
    op.drop_column("users", "is_locked")
    op.drop_constraint("approvals_agent_id_fk", "approvals", type_="foreignkey")
    op.drop_column("approvals", "agent_id")
    op.drop_constraint("agents_template_id_fk", "agents", type_="foreignkey")
    op.drop_column("agents", "config")
    op.drop_column("agents", "template_id")
    op.drop_table("agent_templates")
