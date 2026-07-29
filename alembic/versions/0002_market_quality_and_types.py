"""market quality, expanded market types, and candidate audit records

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Be safe for both historical 0001 databases and fresh metadata-driven 0001 runs."""
    inspector = sa.inspect(op.get_bind())
    market_columns = {column["name"] for column in inspector.get_columns("markets")}
    if "participant" not in market_columns:
        op.add_column("markets", sa.Column("participant", sa.String(128)))
    if "line" not in market_columns:
        op.add_column("markets", sa.Column("line", sa.Numeric(12, 4)))
    if "settlement_rule" not in market_columns:
        op.add_column(
            "markets",
            sa.Column(
                "settlement_rule",
                sa.String(128),
                nullable=False,
                server_default="soccer_regulation",
            ),
        )
    constraints = {
        constraint["name"]: constraint for constraint in inspector.get_unique_constraints("markets")
    }
    old_identity = constraints.get("uq_markets_event_id")
    expected = {"event_id", "market_type", "selection", "participant", "line", "period"}
    if old_identity and set(old_identity["column_names"]) != expected:
        op.drop_constraint("uq_markets_event_id", "markets", type_="unique")
        op.create_unique_constraint(
            "uq_markets_identity",
            "markets",
            ["event_id", "market_type", "selection", "participant", "line", "period"],
        )

    opportunity_columns = {column["name"] for column in inspector.get_columns("opportunities")}
    if "qualification_status" not in opportunity_columns:
        op.add_column(
            "opportunities",
            sa.Column(
                "qualification_status",
                sa.String(32),
                nullable=False,
                server_default="accepted",
            ),
        )
    if "liquidity_qualification" not in opportunity_columns:
        op.add_column(
            "opportunities",
            sa.Column(
                "liquidity_qualification",
                postgresql.JSONB(astext_type=sa.Text()),
            ),
        )

    tables = set(inspector.get_table_names())
    if "order_book_snapshots" not in tables:
        _create_snapshots()
    if "order_book_levels" not in tables:
        _create_levels()
    if "market_candidates" not in tables:
        _create_candidates()


def _create_snapshots() -> None:
    op.create_table(
        "order_book_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_market_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("best_bid", sa.Numeric(12, 8)),
        sa.Column("best_ask", sa.Numeric(12, 8)),
        sa.Column("midpoint", sa.Numeric(12, 8)),
        sa.Column("spread_cents", sa.Numeric(12, 6)),
        sa.Column("trailing_24h_volume_usd", sa.Numeric(20, 4)),
        sa.Column("volume_source", sa.String(32)),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_market_id"], ["provider_markets.id"]),
    )
    op.create_index(
        "ix_order_book_snapshots_source_timestamp",
        "order_book_snapshots",
        ["source_timestamp"],
    )


def _create_levels() -> None:
    op.create_table(
        "order_book_levels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("price", sa.Numeric(12, 8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 4), nullable=False),
        sa.Column("notional_usd", sa.Numeric(20, 4), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["order_book_snapshots.id"]),
        sa.UniqueConstraint("snapshot_id", "side", "price", "quantity"),
    )


def _create_candidates() -> None:
    op.create_table(
        "market_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("prediction_quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sportsbook_quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("edge_percentage_points", sa.Numeric(12, 6), nullable=False),
        sa.Column("rejection_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "liquidity_qualification",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["prediction_quote_id"], ["prediction_market_quotes.id"]),
        sa.ForeignKeyConstraint(["sportsbook_quote_id"], ["sportsbook_quotes.id"]),
        sa.ForeignKeyConstraint(["snapshot_id"], ["order_book_snapshots.id"]),
    )
    op.create_index("ix_market_candidates_accepted", "market_candidates", ["accepted"])
    op.create_index("ix_market_candidates_evaluated_at", "market_candidates", ["evaluated_at"])


def downgrade() -> None:
    op.drop_table("market_candidates")
    op.drop_table("order_book_levels")
    op.drop_table("order_book_snapshots")
    op.drop_constraint("uq_markets_identity", "markets", type_="unique")
    op.create_unique_constraint(
        "uq_markets_event_id",
        "markets",
        ["event_id", "market_type", "selection", "period"],
    )
    op.drop_column("opportunities", "liquidity_qualification")
    op.drop_column("opportunities", "qualification_status")
    op.drop_column("markets", "settlement_rule")
    op.drop_column("markets", "line")
    op.drop_column("markets", "participant")
