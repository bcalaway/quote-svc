"""The job API (app/jobs.py) and metrics (app/metrics.py)."""

from fastapi.testclient import TestClient

from app import jobs
from app.config import Settings
from app.main import app
from tests.test_load import _up

client = TestClient(app)
AUTH = {"Authorization": "Bearer t"}


def _setup(monkeypatch, up=None):
    monkeypatch.setattr(jobs, "settings", Settings(airflow_token="t", read_token="r"))
    up = up or _up()
    monkeypatch.setattr(jobs, "_upstream", lambda: up)
    return up


def test_load_needs_the_token(migrated_db, monkeypatch):
    _setup(monkeypatch)
    assert client.post("/jobs/load").status_code == 401
    assert client.post("/jobs/load", headers={"Authorization": "Bearer r"}).status_code == 401


def test_load_then_reads(migrated_db, monkeypatch):
    _setup(monkeypatch)
    out = client.post("/jobs/load", headers=AUTH).json()
    assert out["sources"][0]["added"] == 3
    read = {"Authorization": "Bearer r"}
    series = client.get("/jobs/series", params={"name": "UST-10Y-CMT", "start": "2026-10-01", "end": "2026-10-31"},
                        headers=read).json()["series"][0]
    assert [p["value"] for p in series["points"]] == ["0.041", "0.0412"]
    curve = client.get("/jobs/curve", params={"name": ["UST-1.5M-CMT", "UST-10Y-CMT"]}, headers=read).json()
    assert curve["as_of"] == "2026-10-02" and curve["missing"] == ["UST-1.5M-CMT"]
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


def test_metrics_without_a_database():
    assert "quote_svc_up 0" in client.get("/metrics").text
