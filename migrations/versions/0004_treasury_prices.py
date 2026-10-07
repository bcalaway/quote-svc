"""treasury prices: source_period.unmapped, instrument_ref.type and status

Phase 3, step 4 (mkt-data's docs/phase-3.md): FedInvest's Treasury prices by
CUSIP. A period remembers how many values it skipped for keys with no
instrument, so it's reloaded once secmaster-svc knows them; instrument_ref
keeps each instrument's type and status, for the prices freshness check.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source_period") as t:
        t.add_column(sa.Column("unmapped", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("instrument_ref") as t:
        t.add_column(sa.Column("type", sa.String(length=30), nullable=False, server_default=""))
        t.add_column(sa.Column("status", sa.String(length=20), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("instrument_ref") as t:
        t.drop_column("status")
        t.drop_column("type")
    with op.batch_alter_table("source_period") as t:
        t.drop_column("unmapped")
