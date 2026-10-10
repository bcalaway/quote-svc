"""Whether the Treasury curve is in on time, and stuck series (app/freshness.py)."""

from datetime import UTC, date, datetime
from decimal import Decimal

from app import db, freshness
from app.models import Golden, Quote
from tests.fakes import FakeCalendars

NY = freshness.NEW_YORK
COLUMBUS = {date(2026, 10, 12)}


def _at(y, m, d, h):
    return datetime(y, m, d, h, tzinfo=NY)


def test_due_date_is_the_last_business_day_past_9am_next_day():
    assert freshness.due_date(_at(2026, 10, 7, 10), set()) == date(2026, 10, 6)  # Wednesday 10 a.m.: Tuesday's
    assert freshness.due_date(_at(2026, 10, 7, 8), set()) == date(2026, 10, 5)  # 8 a.m.: not yet; Monday's
    assert freshness.due_date(_at(2026, 10, 5, 10), set()) == date(2026, 10, 2)  # Monday: Friday's
    assert freshness.due_date(_at(2026, 10, 10, 12), set()) == date(2026, 10, 9)  # Saturday noon: Friday's
    # Columbus Day is closed: on Tuesday the 13th, Friday's curve is still the latest due.
    assert freshness.due_date(_at(2026, 10, 13, 10), COLUMBUS) == date(2026, 10, 9)
    assert freshness.due_date(_at(2026, 10, 13, 10), set()) == date(2026, 10, 12)


def test_repeats():
    assert freshness.repeats([]) == 0
    assert freshness.repeats([Decimal("0.041"), Decimal("0.0412")]) == 1
    assert freshness.repeats([Decimal("0.041")] * 4 + [Decimal("0.04")]) == 4


def _quote(sec_id, day, value, source="UST-PAR"):
    now = datetime(2026, 10, 1, tzinfo=NY)
    return Quote(sec_id=sec_id, source=source, as_of=day, field="yield", value=value, observation_id=1,
                 capture_id=1, loaded_at=now)


def _golden(sec_id, day, value):
    return Golden(sec_id=sec_id, as_of=day, field="yield", value=value, source="UST-PAR",
                  updated_at=datetime(2026, 10, 1, tzinfo=NY))


def test_check(migrated_db):
    with db.session() as s:
        for d in (5, 6, 7, 8, 9):
            s.add(_quote(12, date(2026, 10, d), Decimal("0.041")))
            s.add(_golden(12, date(2026, 10, d), Decimal("0.041")))
        s.add(_quote(2, date(2026, 10, 8), Decimal("0.04")))  # stopped a day early
        s.add(_quote(30, date(2026, 8, 3), Decimal("0.05")))  # long gone: not active
        s.commit()
        out = freshness.check(s, FakeCalendars, _at(2026, 10, 13, 10))
    assert out["due"] == date(2026, 10, 9) and out["calendar_ok"]  # Columbus Day closed in the fake
    assert out["missing"] == {12: False, 2: True}
    assert out["repeats"] == {12: 5, 2: 0}


def test_an_unreachable_calendar_falls_back_to_weekdays(migrated_db):
    class Down(FakeCalendars):
        def closed_days(self, calendar, start, end):
            raise ConnectionError("calendar-svc unreachable")

    with db.session() as s:
        out = freshness.check(s, Down, _at(2026, 10, 13, 10))
    assert out["due"] == date(2026, 10, 12) and not out["calendar_ok"] and out["last"] == {}


def test_fixing_and_positioning_sources_are_late_past_their_limit(migrated_db):
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)  # a Tuesday
    loaded = datetime(2026, 10, 13, tzinfo=UTC)
    with db.session() as s:
        for src, day in (("NYFED-SOFR", date(2026, 10, 9)), ("CFTC-TFF", date(2026, 9, 22)),
                         ("FRB-H10", date(2026, 10, 2))):
            s.add(Quote(sec_id=1, source=src, as_of=day, field="rate", value=Decimal("0.04"), observation_id=1,
                        capture_id=1, loaded_at=loaded))
        s.commit()
        got = freshness.source_dates(s, now)
    assert got["NYFED-SOFR"] == {"last": date(2026, 10, 9), "age": 4, "late": False, "limit": 5}
    assert got["CFTC-TFF"]["age"] == 21 and got["CFTC-TFF"]["late"]  # three weeks: a report missed
    assert got["FRB-H10"]["age"] == 11 and not got["FRB-H10"]["late"]  # H.10 is weekly
    assert got["ECB-EXR"] == {"last": None, "age": None, "late": True, "limit": 6}  # nothing loaded at all
