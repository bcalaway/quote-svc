"""The quote store's tables (mkt-data's docs/phase-2.md, Part B step 5).

Every value is a Decimal in `numeric`, rates as decimals (0.0425 = 4.25%).
Instruments are secmaster-svc's `sec_id`s; `instrument_ref` keeps their short
names locally for logs, metrics and answers, refreshed at every load. Every
change here needs a matching Alembic migration; tests/test_migrations.py
fails if they disagree.

All of it is rebuildable from mkt-data's near-raw observations: a rebuild
clears the watermarks and reloads every period.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Quote(Base):
    """One source's current value for an instrument, date and field, with lineage to near-raw."""

    __tablename__ = "quote"
    __table_args__ = (
        Index("uq_quote", "sec_id", "source", "as_of", "field", unique=True),
        Index("ix_quote_source_as_of", "source", "as_of"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sec_id: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))  # an mkt-data source: UST-PAR, H15-TCM
    as_of: Mapped[date] = mapped_column(Date)
    field: Mapped[str] = mapped_column(String(20))  # yield
    value: Mapped[Decimal] = mapped_column(Numeric)
    observation_id: Mapped[int] = mapped_column(Integer)  # mkt-data's observation row
    capture_id: Mapped[int] = mapped_column(Integer)  # and the raw capture it came from
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QuoteHistory(Base):
    """A quote's earlier value: revised (the source changed it) or removed (the source dropped it)."""

    __tablename__ = "quote_history"
    __table_args__ = (
        CheckConstraint("reason IN ('revised', 'removed')", name="ck_quote_history_reason"),
        Index("ix_quote_history_key", "sec_id", "source", "as_of", "field"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sec_id: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))
    as_of: Mapped[date] = mapped_column(Date)
    field: Mapped[str] = mapped_column(String(20))
    value: Mapped[Decimal] = mapped_column(Numeric)
    observation_id: Mapped[int] = mapped_column(Integer)
    capture_id: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(10))


class Golden(Base):
    """The value to use for an instrument, date and field: the highest-priority source that has one."""

    __tablename__ = "golden"
    __table_args__ = (Index("ix_golden_as_of", "as_of"),)

    sec_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, primary_key=True)
    field: Mapped[str] = mapped_column(String(20), primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric)
    source: Mapped[str] = mapped_column(String(20))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourcePeriod(Base):
    """Watermark: the newest mkt-data capture each source's month was loaded from."""

    __tablename__ = "source_period"

    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    period: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM
    capture_id: Mapped[int] = mapped_column(Integer)
    values: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UnmappedKey(Base):
    """A source key secmaster-svc has no instrument for: its values aren't loaded."""

    __tablename__ = "unmapped_key"

    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(60), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    values: Mapped[int] = mapped_column(Integer)  # in the periods last seen


class InstrumentRef(Base):
    """secmaster-svc's short name for each sec_id, refreshed at every load."""

    __tablename__ = "instrument_ref"

    sec_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    short_name: Mapped[str] = mapped_column(String(40))
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoadRun(Base):
    """Each load: when, how it went, and what it did (for metrics and the job's answer)."""

    __tablename__ = "load_run"
    __table_args__ = (CheckConstraint("outcome IN ('ok', 'error')", name="ck_load_run_outcome"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(8))
    detail: Mapped[str] = mapped_column(Text)  # JSON summary, or the error


class CoverageSeries(Base):
    """One series' coverage against business days (app/coverage.py), replaced on every refresh."""

    __tablename__ = "coverage_series"

    sec_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series: Mapped[str] = mapped_column(String(20), primary_key=True)  # golden, UST-PAR, H15-TCM
    first_date: Mapped[date] = mapped_column(Date)
    last_date: Mapped[date] = mapped_column(Date)
    values: Mapped[int] = mapped_column(Integer)
    missing_days: Mapped[int] = mapped_column(Integer)
    gaps: Mapped[int] = mapped_column(Integer)
    closed_day_values: Mapped[int] = mapped_column(Integer)
    closed_days: Mapped[str] = mapped_column(Text)  # JSON list of dates, first 200
    basis: Mapped[str] = mapped_column(Text)  # which calendar governed which years
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CoverageGap(Base):
    """A run of business days a series has no value for."""

    __tablename__ = "coverage_gap"
    __table_args__ = (Index("ix_coverage_gap_series", "sec_id", "series"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sec_id: Mapped[int] = mapped_column(Integer)
    series: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer)
