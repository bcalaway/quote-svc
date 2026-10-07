"""Treasury price sanity (app/sanity.py): stale days and implausible moves, with the metrics and the history job."""

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from app import db, metrics, sanity
from app.main import app
from app.models import Golden, InstrumentRef

D = Decimal
NOW = datetime(2026, 10, 7, tzinfo=UTC)
BILL, NOTE, BOND = "ust_bill", "ust_note", "ust_bond"


def test_compare_days_counts_unchanged_and_jumps():
    types = {1: BILL, 2: NOTE, 3: BOND, 4: NOTE}
    before = {1: D("99.10"), 2: D("100.5"), 3: D("90"), 4: D("101"), 5: D("50")}
    today = {1: D("99.11"), 2: D("100.5"), 3: D("102.5"), 4: D("106.25")}  # 5 has no price today
    r = sanity.compare_days(today, before, types)
    assert (r["compared"], r["unchanged"], r["unchanged_ratio"]) == (4, 1, D("0.25"))
    assert not r["stale"]  # too few compared to call it
    # The bond moved 12.5 (limit 12), the note 5.25 (limit 5): the note is further past its limit.
    assert [(j["sec_id"], j["change"]) for j in r["jumps"]] == [(4, D("5.25")), (3, D("12.5"))]


def test_a_repeated_page_is_stale():
    before = {i: D(100) + D(i) / 100 for i in range(60)}
    today = dict(before) | {0: D("100.5")}
    r = sanity.compare_days(today, before, {})
    assert r["stale"] and r["unchanged_ratio"] == D("0.9833") and not r["jumps"]
    assert not sanity.compare_days({i: v + D("0.01") for i, v in before.items()}, before, {})["stale"]


def _refs(s):
    for i, (name, t, status) in {1: ("UST-B-2026-12-10", BILL, "active"), 2: ("UST-4.25-2035-08-15", NOTE, "active"),
                                 3: ("UST-4.75-2055-08-15", BOND, "active"),
                                 4: ("UST-2-2026-09-30", NOTE, "matured")}.items():
        s.add(InstrumentRef(sec_id=i, short_name=name, type=t, status=status, refreshed_at=NOW))


def _price(s, sec_id, day, value):
    s.add(Golden(sec_id=sec_id, as_of=day, field="price", value=D(value), source="TD-PRICES", updated_at=NOW))


def _load(s):
    _refs(s)
    for sec_id, prices in {1: ("99.10", "99.11", "99.12"), 2: ("99.5", "99.6", "105.0"),
                           3: ("80", "80.5", "81"), 4: ("99.99", "100", "100")}.items():
        for day, v in zip((date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6)), prices, strict=True):
            _price(s, sec_id, day, v)
    s.commit()


def test_check_compares_the_latest_day_for_active_securities(migrated_db):
    with db.session() as s:
        _load(s)
        r = sanity.check(s, date(2026, 10, 7))  # nothing on the 7th: the 6th against the 5th
    assert (r["day"], r["before"], r["compared"], r["unchanged"]) == (date(2026, 10, 6), date(2026, 10, 5), 3, 0)
    assert [(j["sec_id"], j["change"]) for j in r["jumps"]] == [(2, D("5.4"))]  # the matured note isn't counted
    with db.session() as s:
        assert sanity.check(s, date(2026, 10, 1))["compared"] == 0  # no prices yet


def test_metrics(migrated_db, monkeypatch):
    monkeypatch.setattr(metrics.freshness, "previous_business_day", lambda d, closed: date(2026, 10, 6))
    with db.session() as s:
        _load(s)
        text = metrics.render(s)
    assert "quote_svc_prices_compared 3" in text and "quote_svc_prices_unchanged_ratio 0" in text
    assert "quote_svc_prices_stale 0" in text and "quote_svc_prices_jumps_count 1" in text
    assert 'quote_svc_prices_jump{instrument="UST-4.25-2035-08-15",type="ust_note"} 5.4' in text


def test_history(migrated_db):
    with db.session() as s:
        _load(s)
        r = sanity.history(s, date(2026, 10, 5), date(2026, 10, 6))
    assert r["days"] == 2 and r["stale_days"] == 0
    # The matured note is in history: unchanged on the 6th, one of four.
    assert r["most_unchanged"] == [
        {"day": "2026-10-06", "before": "2026-10-05", "compared": 4, "unchanged_ratio": "0.2500"},
        {"day": "2026-10-05", "before": "2026-10-02", "compared": 4, "unchanged_ratio": "0.0000"}]
    assert r["jump_days"] == [{"day": "2026-10-06", "before": "2026-10-05", "jumps": 1,
                               "worst": "UST-4.25-2035-08-15", "change": "5.4"}]
    assert r["moves"][NOTE]["moves"] == 4 and r["moves"][NOTE]["over_limit"] == 1
    assert r["moves"][NOTE]["larger_than"]["3"] == 1 and r["moves"][BOND]["larger_than"]["0.25"] == 2
    assert r["largest"][NOTE][0] == {"day": "2026-10-06", "security": "UST-4.25-2035-08-15", "before": "99.6",
                                     "after": "105", "change": "5.4"}


def test_history_job(migrated_db, monkeypatch):
    from app import jobs
    from app.config import Settings

    monkeypatch.setattr(jobs, "settings", Settings(airflow_token="t", read_token="r"))
    with db.session() as s:
        _load(s)
    client = TestClient(app)
    auth = {"Authorization": "Bearer t"}
    r = client.post("/jobs/prices/sanity-history", params={"start": "2026-10-01", "end": "2026-10-31"}, headers=auth)
    assert r.status_code == 200 and r.json()["days"] == 2
    bad = client.post("/jobs/prices/sanity-history", params={"start": "2026-10-31", "end": "2026-10-01"}, headers=auth)
    assert bad.status_code == 422
