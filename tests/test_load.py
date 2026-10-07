"""The load job (app/load.py): near-raw observations into quotes and golden values."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import db
from app.load import LoadError, run_load, run_rebuild
from app.models import Golden, LoadRun, Quote, QuoteHistory, SourcePeriod, UnmappedKey
from tests.fakes import FakeUpstream

TEN = 12
D1, D2 = "2026-10-01", "2026-10-02"


def _up():
    up = FakeUpstream()
    up.put("UST-PAR", "2026-10", 32, [("BC_10YEAR", D1, "4.10"), ("BC_10YEAR", D2, "4.12"),
                                      ("BC_1_5MONTH", D1, "4.05"), ("BC_30YEARDISPLAY", D1, "4.80")])
    up.put("H15-TCM", "2026-10", 34, [("RIFLGFCY10_N.B", D1, "4.10"), ("RIFLGFCY10_N.B", D2, "4.13")])
    return up


def _load(up):
    with db.session() as s:
        return run_load(s, up)


def _golden(sec_id, day):
    with db.session() as s:
        g = s.get(Golden, (sec_id, date.fromisoformat(day), "yield"))
        return (g.value, g.source) if g else None


def _count(model, *where):
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def test_first_load(migrated_db):
    out = _load(_up())
    ust, h15, prices = out["sources"]
    assert (ust["added"], ust["reloaded"], ust["unmapped"]) == (3, 1, ["BC_30YEARDISPLAY"])
    assert h15["added"] == 2 and prices["periods"] == 0
    assert _count(Quote) == 5 and _count(UnmappedKey) == 1
    # Percent to decimal, exactly; UST-PAR wins where both have a value.
    assert _golden(TEN, D1) == (Decimal("0.041"), "UST-PAR")
    assert _golden(TEN, D2) == (Decimal("0.0412"), "UST-PAR")
    assert out["golden_set"] == 3


def test_a_failed_month_keeps_the_months_before_it(migrated_db):
    up = _up()
    up.put("UST-PAR", "2026-11", 40, [("BC_10YEAR", "2026-11-02", "x")])
    with pytest.raises(LoadError), db.session() as s:
        run_load(s, up)
    with db.session() as s:
        marks = set(s.scalars(select(SourcePeriod.period)))
    assert marks == {"2026-10"} and _golden(TEN, D1) == (Decimal("0.041"), "UST-PAR")
    up.put("UST-PAR", "2026-11", 41, [("BC_10YEAR", "2026-11-02", "4.20")])
    out = _load(up)
    assert out["sources"][0]["reloaded"] == 1 and _golden(TEN, "2026-11-02") == (Decimal("0.042"), "UST-PAR")


def test_nothing_new_reads_nothing(migrated_db):
    up = _up()
    _load(up)
    up.reads.clear()
    out = _load(up)
    assert up.reads == [] and all(x["reloaded"] == 0 for x in out["sources"])
    assert _count(LoadRun, LoadRun.outcome == "ok") == 2


def test_a_revision_keeps_history_and_moves_golden(migrated_db):
    up = _up()
    _load(up)
    up.put("UST-PAR", "2026-10", 35, [("BC_10YEAR", D1, "4.10"), ("BC_10YEAR", D2, "4.15"), ("BC_1_5MONTH", D1, "4.05")])
    out = _load(up)
    ust = out["sources"][0]
    assert (ust["revised"], ust["removed"], ust["added"]) == (1, 0, 0)
    assert _golden(TEN, D2) == (Decimal("0.0415"), "UST-PAR")
    with db.session() as s:
        h = s.scalar(select(QuoteHistory))
        assert (h.reason, h.value, h.capture_id) == ("revised", Decimal("0.0412"), 32)
        assert s.get(SourcePeriod, ("UST-PAR", "2026-10")).capture_id == 35


def test_a_dropped_value_falls_back_to_the_next_source(migrated_db):
    up = _up()
    _load(up)
    up.put("UST-PAR", "2026-10", 36, [("BC_10YEAR", D1, "4.10"), ("BC_1_5MONTH", D1, "4.05")])
    out = _load(up)
    assert out["sources"][0]["removed"] == 1
    assert _golden(TEN, D2) == (Decimal("0.0413"), "H15-TCM")
    assert _count(QuoteHistory, QuoteHistory.reason == "removed") == 1


def test_a_value_no_source_has_leaves_golden(migrated_db):
    up = _up()
    _load(up)
    up.put("UST-PAR", "2026-10", 37, [("BC_10YEAR", D1, "4.10"), ("BC_10YEAR", D2, "4.12")])
    _load(up)
    assert _golden(2, D1) is None


def test_a_key_that_starts_resolving_is_no_longer_unmapped(migrated_db, monkeypatch):
    up = _up()
    _load(up)
    from tests import fakes
    monkeypatch.setitem(fakes.KEYS["UST-PAR"], "BC_30YEARDISPLAY", 30)
    with db.session() as s:
        run_rebuild(s, up, "UST-PAR")
    assert _count(UnmappedKey) == 0 and _count(Quote, Quote.sec_id == 30) == 1


def test_rebuild_rereads_and_corrects(migrated_db):
    up = _up()
    _load(up)
    with db.session() as s:
        s.query(Quote).filter(Quote.as_of == date(2026, 10, 2), Quote.source == "UST-PAR").delete()
        s.commit()
    up.reads.clear()
    with db.session() as s:
        out = run_rebuild(s, up)
    assert sorted(up.reads) == [("H15-TCM", "2026-10"), ("UST-PAR", "2026-10")]
    assert out["sources"][0]["added"] == 1 and _golden(TEN, D2) == (Decimal("0.0412"), "UST-PAR")
    with db.session() as s, pytest.raises(LoadError, match="unknown source"):
        run_rebuild(s, up, "NOPE")


def test_resolves_each_key_once_per_load(migrated_db):
    up = _up()
    up.put("UST-PAR", "2026-09", 31, [("BC_10YEAR", "2026-09-30", "4.00")])
    _load(up)
    ust = [keys for scheme, keys in up.resolves if scheme == "UST-PAR"]
    assert sum(k.count("BC_10YEAR") for k in ust) == 1


def test_a_failure_is_recorded_and_changes_nothing(migrated_db):
    up = _up()
    up.put("UST-PAR", "2026-10", 32, [("BC_10YEAR", D1, "4.10"), ("BC_10YEAR", D1, "4.11")])
    with pytest.raises(LoadError, match="two values"), db.session() as s:
        run_load(s, up)
    assert _count(Quote) == 0 and _count(SourcePeriod) == 0
    assert _count(LoadRun, LoadRun.outcome == "error") == 1


def test_an_excluded_window_leaves_golden_empty_but_keeps_the_quote(migrated_db, monkeypatch):
    from app import load

    up = _up()
    up.put("UST-PAR", "2026-10", 38, [("BC_10YEAR", D1, "4.10")])
    _load(up)
    assert _golden(TEN, D2) == (Decimal("0.0413"), "H15-TCM")
    # Added after golden was set: the next load takes it back out, with nothing new upstream.
    monkeypatch.setitem(load.GOLDEN_EXCLUDE, "UST-10Y-CMT", [("H15-TCM", date(2026, 10, 2), date(2026, 10, 2), "test")])
    out = _load(up)
    assert out["golden_removed"] == 1 and _golden(TEN, D2) is None
    assert _golden(TEN, D1) == (Decimal("0.041"), "UST-PAR")
    assert _count(Quote, Quote.source == "H15-TCM") == 2  # still loaded and served as H.15's
    # UST-PAR isn't excluded, so a value from it fills the date again.
    up.put("UST-PAR", "2026-10", 39, [("BC_10YEAR", D1, "4.10"), ("BC_10YEAR", D2, "4.12")])
    _load(up)
    assert _golden(TEN, D2) == (Decimal("0.0412"), "UST-PAR")
