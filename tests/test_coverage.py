"""Coverage (app/coverage.py): each series against the business days it should have."""

from datetime import date

from app import coverage, db
from app.load import run_load
from tests.fakes import FakeCalendars, FakeUpstream

TEN = 12


def _up():
    up = FakeUpstream()
    # Thu 1, Fri 2; Mon 5 and Tue 6 missing; Wed 7; Mon 12 is Columbus Day (SIFMA-US closed) but has a value.
    up.put("UST-PAR", "2026-10", 50, [("BC_10YEAR", d, "4.10") for d in
                                      ("2026-10-01", "2026-10-02", "2026-10-07", "2026-10-08", "2026-10-09",
                                       "2026-10-12", "2026-10-13")])
    up.put("H15-TCM", "2026-10", 51, [("RIFLGFCY10_N.B", d, "4.10") for d in ("2026-10-01", "2026-10-02")])
    return up


def _refresh():
    with db.session() as s:
        run_load(s, _up())
    with db.session() as s:
        summary = coverage.refresh(s, FakeCalendars())
        rows = {(r["instrument"], r["series"]): r for r in coverage.report(s)}
    return summary, rows


def test_gaps_and_closed_day_values(migrated_db):
    summary, rows = _refresh()
    ust = rows[("UST-10Y-CMT", "UST-PAR")]
    assert (ust["first"], ust["last"], ust["values"]) == ("2026-10-01", "2026-10-13", 7)
    assert ust["missing_days"] == 2 and ust["gaps"] == [{"start": "2026-10-05", "end": "2026-10-06", "days": 2}]
    assert ust["closed_days"] == ["2026-10-12"] and ust["basis"] == "SIFMA-US 2026"
    golden = rows[("UST-10Y-CMT", "golden")]
    assert golden["values"] == 7 and golden["missing_days"] == 2
    h15 = rows[("UST-10Y-CMT", "H15-TCM")]
    assert h15["missing_days"] == 0 and h15["gaps"] == []
    assert summary["series"] == 3  # only the 10-year has values: golden, UST-PAR, H15-TCM


def test_a_year_no_calendar_covers_is_checked_against_weekdays(migrated_db):
    cal = FakeCalendars()
    cal.covered = {"SIFMA-US": set(), "FED": set()}
    basis = coverage.basis_for(cal, date(2026, 10, 1), date(2026, 10, 31))
    assert basis.is_business_day(date(2026, 10, 12))  # a holiday, but no calendar says so
    assert not basis.is_business_day(date(2026, 10, 10))  # Saturday
    assert basis.describe(date(2026, 10, 1), date(2026, 10, 31)) == "weekdays 2026"


def test_each_year_uses_the_first_calendar_that_covers_it():
    basis = coverage.basis_for(FakeCalendars(), date(2025, 12, 1), date(2026, 1, 31))
    assert basis.by_year == {2025: "FED", 2026: "SIFMA-US"}
    assert not basis.is_business_day(date(2025, 12, 25))
    assert basis.describe(date(2025, 12, 1), date(2026, 1, 31)) == "FED 2025, SIFMA-US 2026"


def test_refresh_replaces_the_previous_result(migrated_db):
    _refresh()
    summary, rows = _refresh()
    assert len(rows) == summary["series"]
