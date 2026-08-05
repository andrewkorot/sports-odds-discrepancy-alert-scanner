"""persist missing aligned sportsbook outcome audits

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "missing_sportsbook_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("prediction_quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bookmaker_id", sa.String(length=64), nullable=False),
        sa.Column("rejection_reason", sa.String(length=64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["prediction_quote_id"], ["prediction_market_quotes.id"]),
        sa.ForeignKeyConstraint(["bookmaker_id"], ["bookmakers.canonical_id"]),
    )
    op.create_index(
        "ix_missing_sportsbook_outcomes_reason",
        "missing_sportsbook_outcomes",
        ["rejection_reason"],
    )
    op.create_index(
        "ix_missing_sportsbook_outcomes_evaluated_at",
        "missing_sportsbook_outcomes",
        ["evaluated_at"],
    )


def downgrade() -> None:
    op.drop_table("missing_sportsbook_outcomes")
