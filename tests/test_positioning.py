"""The CFTC's positioning (mkt-data's docs/phase-4.md, step 5): weekly quotes on each futures product."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import db
from app.load import LoadError, run_load
from app.models import Golden, Quote
from tests.fakes import FakeUpstream

OCT6 = date(2026, 10, 6)


def _up():
    up = FakeUpstream()
    for src, cap, oi in (("CFTC-TFF", 9906, "5742990"), ("CFTC-TFF-COMBINED", 9908, "6005112")):
        up.put(src, "2026-10-06", cap, [
            ("043602", "2026-10-06", "open_interest_all", oi, "contracts"),
            ("043602", "2026-10-06", "lev_money_positions_short", "1484350", "contracts"),
            ("043602", "2026-10-06", "dealer_positions_spread_all", "220485", "contracts"),
            ("043602", "2026-10-06", "traders_asset_mgr_long_all", "81", "traders"),
            ("043602", "2026-10-06", "conc_net_le_8_tdr_short_all", "14.1", "percent"),
            ("13874A", "2026-10-06", "open_interest_all", "1932227", "contracts"),  # E-mini S&P: not ours
        ])
    return up


def test_positions_are_quotes_on_the_product(migrated_db):
    with db.session() as s:
        out = run_load(s, _up())
        got = {(q.source, q.field): q.value for q in s.scalars(select(Quote).where(Quote.sec_id == 301))}
        assert s.scalar(select(func.count()).select_from(Golden).where(Golden.sec_id == 301)) == 0
    assert got[("CFTC-TFF", "oi")] == Decimal(5742990) and got[("CFTC-TFF-COMBINED", "oi")] == Decimal(6005112)
    assert got[("CFTC-TFF", "lev_funds_short")] == Decimal(1484350)
    assert got[("CFTC-TFF", "dealer_spread")] == Decimal(220485)
    assert got[("CFTC-TFF", "tr_asset_mgr_long")] == Decimal(81)
    assert got[("CFTC-TFF", "conc_net8_short")] == Decimal("0.141")  # percent to a decimal, like every rate
    tff = next(e for e in out["sources"] if e["source"] == "CFTC-TFF")
    assert tff["unmapped"] == ["13874A"]


def test_other_markets_are_out_of_scope_not_alerted_on(migrated_db):
    from app import main

    with db.session() as s:
        run_load(s, _up())
    body = TestClient(main.app).get("/metrics").text
    assert 'quote_svc_out_of_scope_keys{source="CFTC-TFF"} 1' in body
    assert 'quote_svc_unmapped_keys{source="CFTC-TFF"}' not in body


def test_a_field_with_no_name_of_ours_stops_the_load(migrated_db):
    up = FakeUpstream()
    up.put("CFTC-TFF", "2026-10-06", 9906, [("043602", "2026-10-06", "change_in_open_interest_all", "5", "contracts")])
    with db.session() as s, pytest.raises(LoadError, match="no name of ours"):
        run_load(s, up)
