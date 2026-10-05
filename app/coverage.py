"""Coverage: each series against the business days it should have (phase 2, steps B6 and B8).

For every instrument and series (golden, and each source on its own), from
its first date to its last:

- the days it has values;
- the business days it's missing, grouped into gaps (date ranges);
- the days it has a value although the market was closed.

A year's business days come from the first calendar that covers it in
CALENDAR_ORDER (calendar-svc: SIFMA-US from 1996, FED from 1986); a year
neither covers is checked against weekdays only, and the row says so in
`basis`. Recomputed after every load from the tables here, so it's cheap and
always current. The results back the B6 report and B8's missing-day alerts.
"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.load import SOURCES
from app.models import CoverageGap, CoverageSeries, Golden, InstrumentRef, Quote

CALENDAR_ORDER = ["SIFMA-US", "FED"]
SERIES = ["golden", *SOURCES]
FIELD = "yield"


class Calendars(Protocol):
    def covered_years(self, calendar: str) -> set[int]: ...
    def closed_days(self, calendar: str, start: date, end: date) -> set[date]: ...


@dataclass
class Basis:
    """Which calendar governs each year, and that calendar's closed days."""

    by_year: dict[int, str]
    closed: dict[str, set[date]]

    def is_business_day(self, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        cal = self.by_year.get(d.year)
        return cal is None or d not in self.closed[cal]

    def describe(self, first: date, last: date) -> str:
        runs, current = [], None
        for y in range(first.year, last.year + 1):
            cal = self.by_year.get(y, "weekdays")
            if current and current[0] == cal:
                current[2] = y
            else:
                current = [cal, y, y]
                runs.append(current)
        return ", ".join(f"{c} {a}" + (f"-{b}" if b != a else "") for c, a, b in runs)


def basis_for(cal: Calendars, first: date, last: date) -> Basis:
    years = {c: cal.covered_years(c) for c in CALENDAR_ORDER}
    by_year = {}
    for y in range(first.year, last.year + 1):
        for c in CALENDAR_ORDER:
            if y in years[c]:
                by_year[y] = c
                break
    used = set(by_year.values())
    closed = {c: cal.closed_days(c, first, last) for c in used}
    return Basis(by_year, closed)


def _dates(s: Session, sec_id: int, series: str) -> list[date]:
    if series == "golden":
        q = select(Golden.as_of).where(Golden.sec_id == sec_id, Golden.field == FIELD)
    else:
        q = select(Quote.as_of).where(Quote.sec_id == sec_id, Quote.field == FIELD, Quote.source == series)
    return sorted(s.scalars(q))


def check(dates: list[date], basis: Basis) -> dict:
    """Gaps (runs of missing business days) and values on closed days, for one series."""
    have = set(dates)
    first, last = dates[0], dates[-1]
    gaps, run, missing = [], None, 0
    d = first
    while d <= last:
        if basis.is_business_day(d):
            if d in have:
                run = None
            else:
                missing += 1
                if run is None:
                    run = [d, d, 0]
                    gaps.append(run)
                run[1] = d
                run[2] += 1
        d += timedelta(days=1)
    closed = [x for x in dates if not basis.is_business_day(x)]
    return {"first": first, "last": last, "values": len(dates), "missing": missing,
            "gaps": [(a, b, n) for a, b, n in gaps], "closed_day_values": closed}


_last_failure: dict = {}


def record_failure(error: str) -> None:
    """Remembered in the process for the metrics (quote_svc_coverage_ok 0) until the next success."""
    _last_failure.update(at=datetime.now(UTC), error=error)


def last_failure() -> dict:
    return dict(_last_failure)


def refresh(s: Session, cal: Calendars, now: datetime | None = None) -> dict:
    """Recompute every series' coverage and replace the tables. Commits."""
    now = now or datetime.now(UTC)
    names = dict(s.execute(select(InstrumentRef.sec_id, InstrumentRef.short_name)).all())
    found = {}
    for sec_id in sorted(names):
        for series in SERIES:
            dates = _dates(s, sec_id, series)
            if dates:
                found[(sec_id, series)] = dates
    if not found:
        return {"series": 0}
    lo = min(d[0] for d in found.values())
    hi = max(d[-1] for d in found.values())
    basis = basis_for(cal, lo, hi)
    s.execute(delete(CoverageGap))
    s.execute(delete(CoverageSeries))
    summary = {"series": 0, "missing_days": 0, "gaps": 0, "closed_day_values": 0}
    for (sec_id, series), dates in found.items():
        r = check(dates, basis)
        s.add(CoverageSeries(
            sec_id=sec_id, series=series, first_date=r["first"], last_date=r["last"], values=r["values"],
            missing_days=r["missing"], gaps=len(r["gaps"]), closed_day_values=len(r["closed_day_values"]),
            closed_days=json.dumps([d.isoformat() for d in r["closed_day_values"][:200]]),
            basis=basis.describe(r["first"], r["last"]), refreshed_at=now,
        ))
        for a, b, n in r["gaps"]:
            s.add(CoverageGap(sec_id=sec_id, series=series, start_date=a, end_date=b, days=n))
        summary["series"] += 1
        summary["missing_days"] += r["missing"]
        summary["gaps"] += len(r["gaps"])
        summary["closed_day_values"] += len(r["closed_day_values"])
    s.commit()
    _last_failure.clear()
    return summary


def report(s: Session) -> list[dict]:
    """The stored coverage, by instrument and series, with each series' gaps."""
    names = dict(s.execute(select(InstrumentRef.sec_id, InstrumentRef.short_name)).all())
    gaps: dict[tuple, list] = {}
    for g in s.scalars(select(CoverageGap).order_by(CoverageGap.start_date)):
        gaps.setdefault((g.sec_id, g.series), []).append(
            {"start": g.start_date.isoformat(), "end": g.end_date.isoformat(), "days": g.days})
    out = []
    for c in s.scalars(select(CoverageSeries).order_by(CoverageSeries.sec_id, CoverageSeries.series)):
        out.append({
            "instrument": names.get(c.sec_id, str(c.sec_id)), "sec_id": c.sec_id, "series": c.series,
            "first": c.first_date.isoformat(), "last": c.last_date.isoformat(), "values": c.values,
            "missing_days": c.missing_days, "closed_day_values": c.closed_day_values,
            "closed_days": json.loads(c.closed_days or "[]"), "basis": c.basis,
            "gaps": gaps.get((c.sec_id, c.series), []),
        })
    return out
