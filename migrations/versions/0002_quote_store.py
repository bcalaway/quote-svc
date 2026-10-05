"""quote store: quote, quote_history, golden, source_period, unmapped_key, instrument_ref, load_run

Replaces the template's example `items` table (mkt-data's docs/phase-2.md,
Part B step 5).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False)


def _quote_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sec_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("field", sa.String(length=20), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("observation_id", sa.Integer(), nullable=False),
        sa.Column("capture_id", sa.Integer(), nullable=False),
        _ts("loaded_at"),
    ]


def upgrade() -> None:
    op.drop_table("items")
    op.create_table("quote", *_quote_columns())
    op.create_index("uq_quote", "quote", ["sec_id", "source", "as_of", "field"], unique=True)
    op.create_index("ix_quote_source_as_of", "quote", ["source", "as_of"])
    op.create_table(
        "quote_history", *_quote_columns(), _ts("superseded_at"),
        sa.Column("reason", sa.String(length=10), nullable=False),
        sa.CheckConstraint("reason IN ('revised', 'removed')", name="ck_quote_history_reason"),
    )
    op.create_index("ix_quote_history_key", "quote_history", ["sec_id", "source", "as_of", "field"])
    op.create_table(
        "golden",
        sa.Column("sec_id", sa.Integer(), primary_key=True),
        sa.Column("as_of", sa.Date(), primary_key=True),
        sa.Column("field", sa.String(length=20), primary_key=True),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        _ts("updated_at"),
    )
    op.create_index("ix_golden_as_of", "golden", ["as_of"])
    op.create_table(
        "source_period",
        sa.Column("source", sa.String(length=20), primary_key=True),
        sa.Column("period", sa.String(length=10), primary_key=True),
        sa.Column("capture_id", sa.Integer(), nullable=False),
        sa.Column("values", sa.Integer(), nullable=False),
        _ts("loaded_at"),
    )
    op.create_table(
        "unmapped_key",
        sa.Column("source", sa.String(length=20), primary_key=True),
        sa.Column("source_key", sa.String(length=60), primary_key=True),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        sa.Column("values", sa.Integer(), nullable=False),
    )
    op.create_table(
        "instrument_ref",
        sa.Column("sec_id", sa.Integer(), primary_key=True),
        sa.Column("short_name", sa.String(length=40), nullable=False),
        _ts("refreshed_at"),
    )
    op.create_table(
        "load_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("outcome", sa.String(length=8), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.CheckConstraint("outcome IN ('ok', 'error')", name="ck_load_run_outcome"),
    )


def downgrade() -> None:
    op.drop_table("load_run")
    op.drop_table("instrument_ref")
    op.drop_table("unmapped_key")
    op.drop_table("source_period")
    op.drop_index("ix_golden_as_of", table_name="golden")
    op.drop_table("golden")
    op.drop_index("ix_quote_history_key", table_name="quote_history")
    op.drop_table("quote_history")
    op.drop_index("ix_quote_source_as_of", table_name="quote")
    op.drop_index("uq_quote", table_name="quote")
    op.drop_table("quote")
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
