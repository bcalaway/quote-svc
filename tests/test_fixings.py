"""The fixings (mkt-data's docs/phase-4.md, step 4): SOFR, EFFR, H.10 and the ECB, as mkt-data parses them."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app import db
from app.load import run_load
from app.models import Golden, Quote
from tests.fakes import FakeUpstream

OCT7 = date(2026, 10, 7)


def _up():
    up = FakeUpstream()
    up.put("NYFED-SOFR", "2026-10", 8375, [
        ("SOFR", "2026-10-07", "percentRate", "3.88", "percent"),
        ("SOFR", "2026-10-07", "percentPercentile99", "3.96", "percent"),
        ("SOFR", "2026-10-07", "volumeInBillions", "2968", "USD billions")])
    up.put("NYFED-EFFR", "2026-10", 8378, [
        ("EFFR", "2026-10-07", "percentRate", "3.88", "percent"),
        ("EFFR", "2026-10-07", "targetRateFrom", "3.75", "percent"),
        ("EFFR", "2026-10-07", "targetRateTo", "4.0", "percent")])
    up.put("FRB-H10-RATES", "2026-10", 8348, [
        ("RXI$US_N.B.EU", "2026-10-07", "value", "1.1259", "EUR currency"),
        ("RXI_N.B.JA", "2026-10-07", "value", "157.8100", "JPY currency"),
        ("RXI_N.B.VE", "2026-10-07", "value", "864.3948", "VEB currency")])
    up.put("FRB-H10", "2026-10", 8390, [("JRXWTFB_N.B", "2026-10-07", "value", "121.5432", "index")])
    up.put("ECB-EXR", "2026-10", 8383, [("EXR.D.JPY.EUR.SP00.A", "2026-10-07", "rate", "178.49", "JPY per EUR")])
    return up


def _quotes(s):
    return {(q.sec_id, q.field): q.value for q in s.scalars(select(Quote).where(Quote.as_of == OCT7))}


def test_fixings_load_as_decimals_in_their_sources_direction(migrated_db):
    out = None
    with db.session() as s:
        out = run_load(s, _up())
        got = _quotes(s)
        golden = {(g.sec_id, g.field): (g.value, g.source) for g in s.scalars(select(Golden).where(Golden.as_of == OCT7))}
    assert got[(201, "rate")] == Decimal("0.0388") and got[(201, "rate_p99")] == Decimal("0.0396")
    assert got[(201, "volume_bn")] == Decimal(2968)
    assert (got[(202, "target_low")], got[(202, "target_high")]) == (Decimal("0.0375"), Decimal("0.04"))
    assert got[(203, "rate")] == Decimal("1.1259") and got[(204, "rate")] == Decimal("157.8100")  # not inverted
    assert got[(205, "index")] == Decimal("121.5432") and got[(206, "rate")] == Decimal("178.49")
    assert golden[(201, "rate")] == (Decimal("0.0388"), "NYFED-SOFR") and golden[(206, "rate")][1] == "ECB-EXR"
    assert (201, "volume_bn") not in golden  # only the rate and the index have golden values
    h10 = next(e for e in out["sources"] if e["source"] == "FRB-H10-RATES")
    assert h10["unmapped"] == ["RXI_N.B.VE"]  # a key secmaster-svc doesn't know is skipped, not failed
