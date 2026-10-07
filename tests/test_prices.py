"""FedInvest's Treasury prices by CUSIP (TD-PRICES; mkt-data's docs/phase-3.md, step 4).

The observations are mkt-data's parse of real FedInvest pages (captures #1270
and #1271 on the hub: 2026-10-05, and 2026-10-06 as first fetched, before its
end-of-day prices were printed), exported with mkt-data's parser.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from app import db, freshness
from app.load import run_load
from app.models import Golden, InstrumentRef, Quote, SourcePeriod, UnmappedKey
from tests import fakes
from tests.fakes import FakeCalendars, FakeUpstream

FIXTURES = Path(__file__).parent / "fixtures"
BILL, NOTE, TIPS = 101, 102, 103
OCT5, OCT6 = date(2026, 10, 5), date(2026, 10, 6)


def _rows(name):
    return [tuple(r) for r in json.loads((FIXTURES / name).read_text())]


OCT5_ROWS = _rows("td_prices_2026_10_05_capture1270_observations.json")
OCT6_FIRST = _rows("td_prices_2026_10_06_capture1271_observations.json")


def _up():
    up = FakeUpstream()
    up.put("TD-PRICES", "2026-10-05", 1270, OCT5_ROWS)
    return up


def _load(up):
    with db.session() as s:
        return run_load(s, up)


def _quote(sec_id, day, field):
    with db.session() as s:
        q = s.scalar(select(Quote).where(Quote.sec_id == sec_id, Quote.as_of == day, Quote.field == field))
        return q.value if q else None


def _golden(sec_id, day, field="price"):
    with db.session() as s:
        g = s.get(Golden, (sec_id, day, field))
        return (g.value, g.source) if g else None


def _count(model, *where):
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def _source(out):
    return next(e for e in out["sources"] if e["source"] == "TD-PRICES")


def test_a_day_of_prices(migrated_db):
    out = _load(_up())
    prices = _source(out)
    cusips = {r[0] for r in OCT5_ROWS}
    assert prices["reloaded"] == 1 and prices["values"] == len(OCT5_ROWS) == 1359
    # The three mapped CUSIPs load; the rest are unmapped until secmaster-svc has them.
    assert prices["unmapped_keys"] == len(cusips) - 3 and len(prices["unmapped"]) == 50
    with db.session() as s:
        mark = s.get(SourcePeriod, ("TD-PRICES", "2026-10-05"))
        assert mark.capture_id == 1270 and mark.unmapped == len(OCT5_ROWS) - _count(Quote)
    # Per 100, exactly as printed; end of day is our price, buy and sell keep their names.
    assert _quote(BILL, OCT5, "price") == Decimal("99.978944")
    assert _quote(BILL, OCT5, "sell") == Decimal("99.968333")
    assert _quote(NOTE, OCT5, "price") is not None and _quote(NOTE, OCT5, "eod") is None
    # Golden: price only, from TD-PRICES; no golden buy or sell.
    assert _golden(BILL, OCT5) == (Decimal("99.978944"), "TD-PRICES")
    assert _golden(BILL, OCT5, "sell") is None
    assert _count(Golden) == 3


def test_the_next_days_end_of_day_arrives_with_the_refetch(migrated_db):
    up = _up()
    up.put("TD-PRICES", "2026-10-06", 1271, OCT6_FIRST)  # fetched the evening of the 6th: buy and sell only
    _load(up)
    assert _quote(BILL, OCT6, "sell") is not None and _golden(BILL, OCT6) is None
    eod = [r for r in OCT5_ROWS if r[2] == "eod"]
    refetch = OCT6_FIRST + [(k, "2026-10-06", "eod", v, u) for k, _, _, v, u in eod]
    up.put("TD-PRICES", "2026-10-06", 1300, refetch)  # the 7th's capture re-fetches the 6th
    up.reads.clear()
    out = _source(_load(up))
    assert up.reads == [("TD-PRICES", "2026-10-06")] and out["added"] == 3
    assert _golden(BILL, OCT6) == (Decimal("99.978944"), "TD-PRICES")


def test_unmapped_days_reload_once_secmaster_knows_the_cusip(migrated_db, monkeypatch):
    up = _up()
    _load(up)
    assert _count(UnmappedKey, UnmappedKey.source_key == "912797VK0") == 1
    monkeypatch.setitem(fakes.KEYS["CUSIP"], "912797VK0", 104)
    monkeypatch.setitem(fakes.NAMES, 104, "UST-B-2026-10-06")
    monkeypatch.setitem(fakes.TYPES, 104, "ust_bill")
    up.reads.clear()
    out = _source(_load(up))  # no new capture: the day reloads because a key now resolves
    assert out["now_resolving"] == 1 and up.reads == [("TD-PRICES", "2026-10-05")]
    assert _quote(104, OCT5, "price") == Decimal("100.000000")
    assert _count(UnmappedKey, UnmappedKey.source_key == "912797VK0") == 0
    # Nothing new: nothing read.
    up.reads.clear()
    _load(up)
    assert up.reads == []


def test_yields_and_prices_side_by_side(migrated_db):
    up = _up()
    up.put("UST-PAR", "2026-10", 32, [("BC_10YEAR", "2026-10-05", "4.10")])
    _load(up)
    assert _golden(12, OCT5, "yield") == (Decimal("0.041"), "UST-PAR")
    assert _golden(BILL, OCT5) == (Decimal("99.978944"), "TD-PRICES")
    with db.session() as s:
        ref = s.get(InstrumentRef, NOTE)
        assert (ref.short_name, ref.type, ref.status) == ("UST-3.5-2028-09-30", "ust_note", "active")


def _at(y, m, d, h):
    return datetime(y, m, d, h, tzinfo=freshness.NEW_YORK)


def test_prices_freshness(migrated_db, monkeypatch):
    monkeypatch.setattr(freshness, "_cache", {})
    up = _up()
    up.status[TIPS] = "matured"
    _load(up)
    with db.session() as s:
        # Wednesday the 7th, 10 a.m.: the curve due is Tuesday's, prices Monday's (printed Tuesday evening).
        out = freshness.check_prices(s, FakeCalendars, _at(2026, 10, 7, 10))
        assert out["due"] == OCT5 and out["missing"] == [] and set(out["last"]) == {BILL, NOTE}  # matured: left out
        # Thursday the 8th: Tuesday's prices are due, and only Monday's are in.
        out = freshness.check_prices(s, FakeCalendars, _at(2026, 10, 8, 10))
        assert out["due"] == OCT6 and out["missing"] == [BILL, NOTE]
        # Tuesday the 13th (Columbus Day closed in the fake): Friday's print Tuesday evening, so Thursday's are due.
        assert freshness.check_prices(s, FakeCalendars, _at(2026, 10, 13, 10))["due"] == date(2026, 10, 8)
