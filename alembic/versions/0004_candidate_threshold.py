"""persist configured candidate edge threshold

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("market_candidates")
    }
    if "configured_threshold" not in columns:
        op.add_column(
            "market_candidates",
            sa.Column(
                "configured_threshold",
                sa.Numeric(12, 6),
                nullable=False,
                server_default="3.0",
            ),
        )


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("market_candidates")
    }
    if "configured_threshold" in columns:
        op.drop_column("market_candidates", "configured_threshold")
