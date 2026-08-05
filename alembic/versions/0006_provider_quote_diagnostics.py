"""persist raw provider quote diagnostics

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, column_type in (
        ("provider_source_market_id", sa.String(length=256)),
        ("provider_market_name", sa.Text()),
        ("provider_market_type", sa.String(length=128)),
        ("provider_outcome_id", sa.String(length=256)),
        ("provider_outcome_name", sa.Text()),
    ):
        op.add_column("prediction_market_quotes", sa.Column(name, column_type, nullable=True))

    for name, column_type in (
        ("provider_market_id", sa.String(length=128)),
        ("provider_market_name", sa.Text()),
        ("provider_market_type", sa.String(length=128)),
        ("provider_outcome_id", sa.String(length=256)),
        ("provider_outcome_name", sa.Text()),
        ("bookmaker_outcome_id", sa.String(length=256)),
    ):
        op.add_column("sportsbook_quotes", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    for name in (
        "bookmaker_outcome_id",
        "provider_outcome_name",
        "provider_outcome_id",
        "provider_market_type",
        "provider_market_name",
        "provider_market_id",
    ):
        op.drop_column("sportsbook_quotes", name)
    for name in (
        "provider_outcome_name",
        "provider_outcome_id",
        "provider_market_type",
        "provider_market_name",
        "provider_source_market_id",
    ):
        op.drop_column("prediction_market_quotes", name)
