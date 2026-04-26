"""v4 governance: blocked_domains denylist + seed

Revision ID: 0019_blocked_domains
Revises: 0018_tool_credentials_v2
Create Date: 2026-04-26

Egress denylist consulted later by ``app/services/http_client.py`` (Step 9)
for SSRF / DNS-rebinding defence. Seeded with:
  - Loopback / link-local / RFC1918 ranges (host suffix match isn't enough
    for IP literals — http_client also resolves and re-checks at connect time)
  - Cloud-provider metadata services (AWS / GCP / Azure / DO / Hetzner)
  - .internal and .local TLDs commonly used for internal services

Rows are advisory at this stage — nothing reads them until the hardened
HTTP client lands. Admins can extend / override entries from the admin UI
(future step). Schema is additive.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


revision = "0019_blocked_domains"
down_revision = "0018_tool_credentials_v2"
branch_labels = None
depends_on = None


_SEED_ROWS = [
    # IP literals / private ranges
    ("127.0.0.0/8", "cidr", "Loopback IPv4", "default"),
    ("::1/128", "cidr", "Loopback IPv6", "default"),
    ("10.0.0.0/8", "cidr", "RFC1918 private", "default"),
    ("172.16.0.0/12", "cidr", "RFC1918 private", "default"),
    ("192.168.0.0/16", "cidr", "RFC1918 private", "default"),
    ("169.254.0.0/16", "cidr", "Link-local / cloud metadata", "default"),
    ("fc00::/7", "cidr", "IPv6 unique local", "default"),
    ("fe80::/10", "cidr", "IPv6 link-local", "default"),
    ("0.0.0.0/8", "cidr", "Unspecified IPv4", "default"),
    # Cloud metadata endpoints (host suffix match catches resolution targets)
    ("metadata.google.internal", "host_suffix", "GCP metadata", "default"),
    ("metadata.goog", "host_suffix", "GCP metadata alias", "default"),
    ("169.254.169.254", "host_suffix", "AWS/Azure/DO metadata IP literal", "default"),
    ("metadata.azure.com", "host_suffix", "Azure metadata", "default"),
    # Internal / private TLDs
    (".internal", "host_suffix", "Internal-only TLD", "default"),
    (".local", "host_suffix", "mDNS / local TLD", "default"),
    (".localhost", "host_suffix", "Localhost TLD", "default"),
    (".lan", "host_suffix", "LAN TLD", "default"),
    (".intranet", "host_suffix", "Intranet TLD", "default"),
]


def upgrade() -> None:
    blocked = op.create_table(
        "blocked_domains",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # The pattern to match (host, host suffix, or CIDR) per ``kind``.
        sa.Column("pattern", sa.String(length=255), nullable=False),
        # kind = "host" | "host_suffix" | "cidr"
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="host_suffix"),
        sa.Column("reason", sa.String(length=500), nullable=True),
        # source = "default" (seeded) | "admin" | "user"
        sa.Column("source", sa.String(length=24), nullable=False, server_default="admin"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("pattern", "kind", name="uq_blocked_domains_pattern_kind"),
    )
    op.create_index("ix_blocked_domains_kind_enabled", "blocked_domains", ["kind", "enabled"])

    # Seed defaults. Use a lightweight ad-hoc table for the bulk_insert so
    # alembic can run this offline if needed.
    seed_table = table(
        "blocked_domains",
        column("pattern", sa.String),
        column("kind", sa.String),
        column("reason", sa.String),
        column("source", sa.String),
    )
    op.bulk_insert(
        seed_table,
        [
            {"pattern": p, "kind": k, "reason": r, "source": s}
            for (p, k, r, s) in _SEED_ROWS
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_blocked_domains_kind_enabled", table_name="blocked_domains")
    op.drop_table("blocked_domains")
