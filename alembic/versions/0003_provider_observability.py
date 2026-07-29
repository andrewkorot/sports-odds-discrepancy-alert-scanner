"""provider modes and observability counters

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("connector_health")
    }
    additions = [
        sa.Column("mode", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("connected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_payload_timestamp", sa.DateTime(timezone=True)),
        sa.Column("last_order_book_timestamp", sa.DateTime(timezone=True)),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("events_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("markets_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("books_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trades_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latest_error_code", sa.String(64)),
    ]
    for column in additions:
        if column.name not in columns:
            op.add_column("connector_health", column)


def downgrade() -> None:
    for name in (
        "latest_error_code",
        "trades_processed",
        "books_updated",
        "markets_discovered",
        "events_discovered",
        "stale",
        "last_order_book_timestamp",
        "last_payload_timestamp",
        "connected",
        "enabled",
        "mode",
    ):
        op.drop_column("connector_health", name)
