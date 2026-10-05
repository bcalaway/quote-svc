"""Reads (app/quotes.py): what mkt-api asks."""

from datetime import date

import pytest

from app import db, quotes
from app.load import run_load
from tests.test_load import _up

TEN, SIX_W = 12, 2


@pytest.fixture
def loaded(migrated_db):
    with db.session() as s:
        run_load(s, _up())


def test_names(loaded):
    with db.session() as s:
        assert quotes.sec_ids_for(s, ["ust-10y-cmt", "2"]) == [TEN, SIX_W]
        with pytest.raises(quotes.UnknownInstrument, match="NOPE"):
            quotes.sec_ids_for(s, ["NOPE"])


def test_series_golden_or_one_source(loaded):
    with db.session() as s:
        golden = quotes.series(s, [TEN], date(2026, 10, 1), date(2026, 10, 31))[0]
        h15 = quotes.series(s, [TEN], date(2026, 10, 1), date(2026, 10, 31), source="H15-TCM")[0]
    assert golden["short_name"] == "UST-10Y-CMT"
    assert [(p["as_of"], p["value"], p["source"]) for p in golden["points"]] == [
        ("2026-10-01", "0.041", "UST-PAR"), ("2026-10-02", "0.0412", "UST-PAR")]
    assert [p["value"] for p in h15["points"]] == ["0.041", "0.0413"]


def test_curve_latest_and_on_a_date(loaded):
    with db.session() as s:
        latest = quotes.curve(s, [SIX_W, TEN])
        first = quotes.curve(s, [SIX_W, TEN], date(2026, 10, 1))
        none = quotes.curve(s, [TEN], date(2026, 9, 1))
    assert latest["as_of"] == "2026-10-02" and [p["short_name"] for p in latest["points"]] == ["UST-10Y-CMT"]
    assert latest["missing"] == ["UST-1.5M-CMT"]
    assert [p["short_name"] for p in first["points"]] == ["UST-1.5M-CMT", "UST-10Y-CMT"]
    assert none["points"] == [] and none["missing"] == ["UST-10Y-CMT"]


def test_compare(loaded):
    with db.session() as s:
        rows = quotes.compare(s, [TEN], date(2026, 10, 1), date(2026, 10, 31))
        diffs = quotes.compare(s, [TEN], date(2026, 10, 1), date(2026, 10, 31), only_differences=True)
    assert [r["differs"] for r in rows] == [False, True]
    assert diffs[0]["as_of"] == "2026-10-02"
    assert diffs[0]["values"] == [{"source": "UST-PAR", "value": "0.0412"}, {"source": "H15-TCM", "value": "0.0413"}]


def test_latest(loaded):
    with db.session() as s:
        out = quotes.latest(s, [TEN, 99])
    assert out[0] | {} == {"sec_id": TEN, "short_name": "UST-10Y-CMT", "as_of": "2026-10-02", "value": "0.0412",
                           "source": "UST-PAR"}
    assert out[1]["as_of"] == ""
