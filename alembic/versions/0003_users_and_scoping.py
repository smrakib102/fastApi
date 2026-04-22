"""users and user scoping

Revision ID: 0003_users_and_scoping
Revises: 0002_google_accounts
Create Date: 2026-04-22

"""
from alembic import op
import sqlalchemy as sa

revision = "0003_users_and_scoping"
down_revision = "0002_google_accounts"
branch_labels = None
depends_on = None


LEGACY_EMAIL = "legacy@local"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("hashed_password", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
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
        sa.UniqueConstraint("email"),
    )

    op.create_table(
        "user_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("user_id", "key"),
    )

    op.add_column("agents", sa.Column("user_id", sa.Integer(), nullable=True))
    op.add_column("approvals", sa.Column("user_id", sa.Integer(), nullable=True))
    op.add_column("google_accounts", sa.Column("user_id", sa.Integer(), nullable=True))
    op.add_column("employees", sa.Column("user_id", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("user_id", sa.Integer(), nullable=True))
    op.add_column("reminders", sa.Column("user_id", sa.Integer(), nullable=True))

    op.create_foreign_key("agents_user_id_fk", "agents", "users", ["user_id"], ["id"])
    op.create_foreign_key("approvals_user_id_fk", "approvals", "users", ["user_id"], ["id"])
    op.create_foreign_key(
        "google_accounts_user_id_fk", "google_accounts", "users", ["user_id"], ["id"]
    )
    op.create_foreign_key("employees_user_id_fk", "employees", "users", ["user_id"], ["id"])
    op.create_foreign_key("tasks_user_id_fk", "tasks", "users", ["user_id"], ["id"])
    op.create_foreign_key("reminders_user_id_fk", "reminders", "users", ["user_id"], ["id"])

    op.execute(
        "INSERT INTO users (email, full_name, hashed_password, is_active, is_admin) "
        f"VALUES ('{LEGACY_EMAIL}', 'Legacy User', '', true, false) "
        "ON CONFLICT (email) DO NOTHING"
    )

    op.execute(
        f"UPDATE agents SET user_id = (SELECT id FROM users WHERE email = '{LEGACY_EMAIL}') "
        "WHERE user_id IS NULL"
    )
    op.execute(
        f"UPDATE approvals SET user_id = (SELECT id FROM users WHERE email = '{LEGACY_EMAIL}') "
        "WHERE user_id IS NULL"
    )
    op.execute(
        f"UPDATE google_accounts SET user_id = (SELECT id FROM users WHERE email = '{LEGACY_EMAIL}') "
        "WHERE user_id IS NULL"
    )
    op.execute(
        f"UPDATE employees SET user_id = (SELECT id FROM users WHERE email = '{LEGACY_EMAIL}') "
        "WHERE user_id IS NULL"
    )
    op.execute(
        f"UPDATE tasks SET user_id = (SELECT id FROM users WHERE email = '{LEGACY_EMAIL}') "
        "WHERE user_id IS NULL"
    )
    op.execute(
        f"UPDATE reminders SET user_id = (SELECT id FROM users WHERE email = '{LEGACY_EMAIL}') "
        "WHERE user_id IS NULL"
    )


def downgrade() -> None:
    op.drop_constraint("reminders_user_id_fk", "reminders", type_="foreignkey")
    op.drop_constraint("tasks_user_id_fk", "tasks", type_="foreignkey")
    op.drop_constraint("employees_user_id_fk", "employees", type_="foreignkey")
    op.drop_constraint("google_accounts_user_id_fk", "google_accounts", type_="foreignkey")
    op.drop_constraint("approvals_user_id_fk", "approvals", type_="foreignkey")
    op.drop_constraint("agents_user_id_fk", "agents", type_="foreignkey")

    op.drop_column("reminders", "user_id")
    op.drop_column("tasks", "user_id")
    op.drop_column("employees", "user_id")
    op.drop_column("google_accounts", "user_id")
    op.drop_column("approvals", "user_id")
    op.drop_column("agents", "user_id")

    op.drop_table("user_profiles")
    op.drop_table("users")
