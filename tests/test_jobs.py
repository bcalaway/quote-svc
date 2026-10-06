"""The job API (app/jobs.py) and metrics (app/metrics.py)."""

from fastapi.testclient import TestClient

from app import jobs
from app.config import Settings
from app.main import app
from tests.fakes import FakeCalendars
from tests.test_load import _up

client = TestClient(app)
AUTH = {"Authorization": "Bearer t"}


def _setup(monkeypatch, up=None):
    monkeypatch.setattr(jobs, "settings", Settings(airflow_token="t", read_token="r"))
    up = up or _up()
    monkeypatch.setattr(jobs, "_upstream", lambda: up)
    monkeypatch.setattr(jobs, "_calendars", FakeCalendars)
    return up


def test_load_needs_the_token(migrated_db, monkeypatch):
    _setup(monkeypatch)
    assert client.post("/jobs/load").status_code == 401
    assert client.post("/jobs/load", headers={"Authorization": "Bearer r"}).status_code == 401


def test_load_then_reads(migrated_db, monkeypatch):
    _setup(monkeypatch)
    out = client.post("/jobs/load", headers=AUTH).json()
    assert out["sources"][0]["added"] == 3 and out["coverage"]["series"] == 5  # 10Y: golden, UST-PAR, H15-TCM; 1.5M: golden, UST-PAR
    read = {"Authorization": "Bearer r"}
    series = client.get("/jobs/series", params={"name": "UST-10Y-CMT", "start": "2026-10-01", "end": "2026-10-31"},
                        headers=read).json()["series"][0]
    assert [p["value"] for p in series["points"]] == ["0.041", "0.0412"]
    curve = client.get("/jobs/curve", params={"name": ["UST-1.5M-CMT", "UST-10Y-CMT"]}, headers=read).json()
    assert curve["as_of"] == "2026-10-02" and curve["missing"] == ["UST-1.5M-CMT"]
    bars = client.get("/jobs/bars", params={"name": "UST-10Y-CMT", "start": "2026-10-01", "end": "2026-10-31",
                                           "interval": "month"}, headers=read).json()["series"][0]["bars"]
    assert [(b["start"], b["open"], b["close"]) for b in bars] == [("2026-10-01", "0.041", "0.0412")]
    assert client.get("/jobs/bars", params={"name": "UST-10Y-CMT", "start": "2026-10-01", "end": "2026-10-31",
                                            "interval": "hour"}, headers=read).status_code == 422
    diffs = client.get("/jobs/compare", params={"name": "UST-10Y-CMT", "start": "2026-10-01", "end": "2026-10-31",
                                                "only_differences": True}, headers=read).json()["rows"]
    assert len(diffs) == 1
    assert client.get("/jobs/latest", params={"name": "NOPE"}, headers=read).status_code == 404
    assert client.post("/jobs/rebuild", params={"source": "ust-par"}, headers=AUTH).status_code == 200


def test_a_failed_load_is_a_502(migrated_db, monkeypatch):
    up = _up()
    up.put("UST-PAR", "2026-10", 1, [("BC_10YEAR", "2026-10-01", "x")])
    _setup(monkeypatch, up)
    r = client.post("/jobs/load", headers=AUTH)
    assert r.status_code == 502 and "load failed" in r.json()["detail"]


def test_metrics(migrated_db, monkeypatch):
    _setup(monkeypatch)
    client.post("/jobs/load", headers=AUTH)
    body = client.get("/metrics").text
    assert "quote_svc_up 1" in body and "quote_svc_load_ok 1" in body
    assert 'quote_svc_quotes{source="UST-PAR"} 3' in body
    assert 'quote_svc_golden_values{instrument="UST-10Y-CMT",source="UST-PAR"} 2' in body
    assert 'quote_svc_source_disagreements{instrument="UST-10Y-CMT"} 1' in body
    assert 'quote_svc_unmapped_key{source="UST-PAR",key="BC_30YEARDISPLAY"} 1' in body
    assert 'quote_svc_golden_last_date_timestamp_seconds{instrument="UST-10Y-CMT"} 1790899200' in body
    assert 'quote_svc_source_disagreement_bp{instrument="UST-10Y-CMT",date="2026-10-02",ust_par="0.0412",h15_tcm="0.0413"} -1' in body
    assert 'quote_svc_coverage_values{instrument="UST-10Y-CMT",series="golden"} 2' in body
    assert 'quote_svc_coverage_basis{instrument="UST-10Y-CMT",series="UST-PAR",basis="SIFMA-US 2026"} 1' in body
    assert "quote_svc_coverage_ok 1" in body
    assert "quote_svc_curve_calendar_ok 1" in body and "quote_svc_curve_due_date_timestamp_seconds " in body
    assert "quote_svc_curve_active_instruments " in body  # the values depend on today's date: test_freshness.py


def test_coverage_endpoint_and_a_calendar_outage(migrated_db, monkeypatch):
    _setup(monkeypatch)

    class Down(FakeCalendars):
        def covered_years(self, calendar):
            raise ConnectionError("calendar-svc unreachable")

    monkeypatch.setattr(jobs, "_calendars", Down)
    out = client.post("/jobs/load", headers=AUTH).json()
    assert out["sources"][0]["added"] == 3 and "unreachable" in out["coverage"]["error"]
    assert "quote_svc_coverage_ok 0" in client.get("/metrics").text
    monkeypatch.setattr(jobs, "_calendars", FakeCalendars)
    assert client.post("/jobs/coverage", headers=AUTH).json()["series"] == 5
    rows = client.get("/jobs/coverage", params={"name": "ust-10y-cmt"}, headers={"Authorization": "Bearer r"}).json()
    assert {r["series"] for r in rows["coverage"]} == {"golden", "UST-PAR", "H15-TCM"}


def test_metrics_list_closed_day_values(migrated_db):
    from datetime import UTC, date, datetime

    from app import db
    from app.models import CoverageSeries

    with db.session() as s:
        s.add(CoverageSeries(sec_id=1, series="UST-PAR", first_date=date(2026, 10, 1), last_date=date(2026, 10, 13),
                             values=7, missing_days=0, gaps=0, closed_day_values=1, closed_days='["2026-10-12"]',
                             basis="SIFMA-US 2026", refreshed_at=datetime.now(UTC)))
        s.commit()
    body = client.get("/metrics").text
    assert 'quote_svc_coverage_closed_day{instrument="1",series="UST-PAR",date="2026-10-12"} 1' in body


def test_metrics_without_a_database():
    assert "quote_svc_up 0" in client.get("/metrics").text
