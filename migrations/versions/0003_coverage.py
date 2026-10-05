"""coverage: coverage_series and coverage_gap (each series against business days)

Phase 2, steps B6 and B8 (mkt-data's docs/phase-2.md). Derived tables,
replaced on every refresh.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coverage_series",
        sa.Column("sec_id", sa.Integer(), primary_key=True),
        sa.Column("series", sa.String(length=20), primary_key=True),
        sa.Column("first_date", sa.Date(), nullable=False),
        sa.Column("last_date", sa.Date(), nullable=False),
        sa.Column("values", sa.Integer(), nullable=False),
        sa.Column("missing_days", sa.Integer(), nullable=False),
        sa.Column("gaps", sa.Integer(), nullable=False),
        sa.Column("closed_day_values", sa.Integer(), nullable=False),
        sa.Column("closed_days", sa.Text(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "coverage_gap",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sec_id", sa.Integer(), nullable=False),
        sa.Column("series", sa.String(length=20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
    )
    op.create_index("ix_coverage_gap_series", "coverage_gap", ["sec_id", "series"])


def downgrade() -> None:
    op.drop_index("ix_coverage_gap_series", table_name="coverage_gap")
    op.drop_table("coverage_gap")
    op.drop_table("coverage_series")
