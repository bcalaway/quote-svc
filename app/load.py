"""The load job: mkt-data's near-raw observations into quotes and golden values.

Treasury CMT yields (UST-PAR, H15-TCM; mkt-data's docs/phase-2.md, B5),
FedInvest's Treasury prices by CUSIP (TD-PRICES; docs/phase-3.md, step 4) and
the fixings (docs/phase-4.md, step 4): SOFR and EFFR with their percentiles,
volume and target range, the H.10 rates and dollar indexes, and the ECB's
reference rates, each FX rate in its source's own direction.
For each source, in priority order:

1. List its periods from mkt-data (a month for the CMTs, a day for
   TD-PRICES), each with `latest_capture_id`, the newest capture any of its
   values came from. A period whose capture id differs from the watermark
   here (or is new) is reloaded; the rest are skipped. A revision or a
   dropped value can only come from a newer capture of that period, so it
   always moves the id.
2. Read the period's current values and map each source key to an
   instrument through secmaster-svc (one batch per source and load), in the
   source's scheme: the source's own name for the CMTs, CUSIP for
   TD-PRICES. Keys with no instrument are recorded in `unmapped_key` and
   skipped, and the period remembers how many values it skipped. When an
   unmapped key starts resolving (secmaster-svc loaded the security), every
   period of that source with skipped values is reloaded.
3. Convert to decimals (`percent` / 100 and `per_100` as is, exact), rename
   the field where the source's name isn't ours (TD-PRICES `eod` is
   `price`; `buy` and `sell` keep theirs), and diff against the period's
   quotes: a new value is inserted; a changed one moves the old row to
   `quote_history` as revised; a value the source no longer has moves there
   as removed.

With each period, golden values are recomputed for every (sec_id, date,
field) it touched, for the fields in PRIORITY: the highest-priority source
with a value wins (yields: UST-PAR, then H15-TCM; H.15 fills the years
before 1990 and is a cross-check after; prices: TD-PRICES's end of day).
FedInvest's buy and sell prices are kept as its quotes, with no golden.
A source can be kept out of golden for an instrument over a window
(`GOLDEN_EXCLUDE`): its quotes are still loaded and served as that source's,
but golden leaves the dates empty if no other source has them. Every load
recomputes those windows, so a change to the list takes effect at the next
load without a rebuild.

Each period commits on its own (quotes, golden values, watermark), so a
full-history load stays small in memory and a failure keeps what's done.

Idempotent: a second run with nothing new reads only the period lists. A
rebuild clears the watermarks, so every period is re-read and corrected.
"""

import json
import re
from calendar import monthrange
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Golden, InstrumentRef, LoadRun, Quote, QuoteHistory, SourcePeriod, UnmappedKey
from app.upstream import Upstream

# The fixings (mkt-data's docs/phase-4.md, step 4): the New York Fed's SOFR and EFFR, the Fed's H.10
# rates and dollar indexes, the ECB's euro reference rates. Each instrument has one source, so its golden
# value is that source's. No averages (the SOFR Averages and Index): analytics, for a later phase.
FIXING_SOURCES = ["NYFED-SOFR", "NYFED-EFFR", "FRB-H10-RATES", "FRB-H10", "ECB-EXR"]
# Source priority per field, highest first. Only these fields have golden values.
PRIORITY = {"yield": ["UST-PAR", "H15-TCM"], "price": ["TD-PRICES"],
            "rate": ["NYFED-SOFR", "NYFED-EFFR", "FRB-H10-RATES", "ECB-EXR"], "index": ["FRB-H10"]}
CMT_SOURCES = ["UST-PAR", "H15-TCM"]
SOURCES = [*CMT_SOURCES, "TD-PRICES", *FIXING_SOURCES]
# The secmaster-svc scheme a source's keys resolve in; the source's own name if not listed.
SCHEMES = {"TD-PRICES": "CUSIP"}
# The New York Fed's field names, as mkt-data keeps them, and ours.
NYFED_FIELDS = {"percentRate": "rate", "percentPercentile1": "rate_p1", "percentPercentile25": "rate_p25",
                "percentPercentile75": "rate_p75", "percentPercentile99": "rate_p99", "volumeInBillions": "volume_bn",
                "targetRateFrom": "target_low", "targetRateTo": "target_high", "intraDayHigh": "intraday_high",
                "intraDayLow": "intraday_low", "stdDeviation": "std_dev"}
# A source's field names that aren't ours: (source, its field) -> ours.
FIELDS = {("TD-PRICES", "eod"): "price", ("FRB-H10-RATES", "value"): "rate", ("FRB-H10", "value"): "index",
          **{(src, k): v for src in ("NYFED-SOFR", "NYFED-EFFR") for k, v in NYFED_FIELDS.items()}}
# Source values golden doesn't use, by instrument short name: (source, first, last, why).
GOLDEN_EXCLUDE: dict[str, list[tuple[str, date, date, str]]] = {
    # Bill, 2026-10-05: leave Treasury's 30-year gap in golden. H.15 has values on
    # these dates, but Treasury published no 30-year CMT then (secmaster-svc's
    # note gap-2002-2006), so they aren't published market values.
    "UST-30Y-CMT": [("H15-TCM", date(2002, 2, 19), date(2006, 2, 8), "Treasury's 30-year gap, 2002-2006")],
}
# Dates per query when recomputing golden values (a backfill touches decades).
GOLDEN_BATCH = 500
# Near-raw units, and how to turn one into a decimal.
UNITS = {"percent": Decimal(100), "per_100": Decimal(1),  # prices stay per 100 of face
         "USD billions": Decimal(1), "index": Decimal(1)}
# FX rates stay as printed, in the source's own direction: H.10's "JPY currency" (yen per dollar, or dollars
# per unit for its $US series) and the ECB's "JPY per EUR".
FX_UNIT = re.compile(r"^[A-Z]{3} (currency|per [A-Z]{3})$")


class LoadError(RuntimeError):
    pass


def _span(period: str) -> tuple[date, date]:
    """A period's first and last day: YYYY, YYYY-MM or YYYY-MM-DD."""
    parts = [int(x) for x in period.split("-")]
    if len(parts) == 1:
        return date(parts[0], 1, 1), date(parts[0], 12, 31)
    if len(parts) == 2:
        y, m = parts
        return date(y, m, 1), date(y, m, monthrange(y, m)[1])
    d = date(*parts)
    return d, d


def _to_decimal(value: str, unit: str) -> Decimal:
    if FX_UNIT.match(unit):
        return Decimal(value)
    if unit not in UNITS:
        raise LoadError(f"unit {unit!r} has no conversion; known: {sorted(UNITS)}")
    return Decimal(value) / UNITS[unit]


def _history(q: Quote, now: datetime, reason: str) -> QuoteHistory:
    return QuoteHistory(
        sec_id=q.sec_id, source=q.source, as_of=q.as_of, field=q.field, value=q.value,
        observation_id=q.observation_id, capture_id=q.capture_id, loaded_at=q.loaded_at,
        superseded_at=now, reason=reason,
    )


def _load_period(s: Session, up: Upstream, source: str, period: str, mapping: dict[str, int | None],
                 unmapped: dict[str, int], now: datetime) -> tuple[dict, set]:
    values = up.get_period(source, period)
    new_keys = sorted({v.source_key for v in values} - set(mapping))
    if new_keys:
        found, unknown = up.resolve(SCHEMES.get(source, source), new_keys)
        mapping.update(found)
        mapping.update(dict.fromkeys(unknown))
    want: dict[tuple, tuple] = {}
    for v in values:
        sec_id = mapping.get(v.source_key)
        if sec_id is None:
            unmapped[v.source_key] = unmapped.get(v.source_key, 0) + 1
            continue
        key = (sec_id, date.fromisoformat(v.as_of), FIELDS.get((source, v.field), v.field))
        if key in want:
            raise LoadError(f"{source} {period}: two values for {v.source_key} on {v.as_of} ({v.field})")
        want[key] = (_to_decimal(v.value, v.unit), v.observation_id, v.capture_id)

    first, last = _span(period)
    have = {(q.sec_id, q.as_of, q.field): q for q in s.scalars(select(Quote).where(
        Quote.source == source, Quote.as_of >= first, Quote.as_of <= last))}
    out = {"added": 0, "revised": 0, "removed": 0}
    touched = set()
    for key, (value, obs_id, cap_id) in want.items():
        q = have.get(key)
        if q is None:
            s.add(Quote(sec_id=key[0], source=source, as_of=key[1], field=key[2], value=value,
                        observation_id=obs_id, capture_id=cap_id, loaded_at=now))
            out["added"] += 1
            touched.add(key)
        elif q.value != value:
            s.add(_history(q, now, "revised"))
            q.value, q.observation_id, q.capture_id, q.loaded_at = value, obs_id, cap_id, now
            out["revised"] += 1
            touched.add(key)
        elif (q.observation_id, q.capture_id) != (obs_id, cap_id):
            # Same value, new lineage (mkt-data rebuilt its near-raw rows): no history.
            q.observation_id, q.capture_id = obs_id, cap_id
    for key, q in have.items():
        if key not in want:
            s.add(_history(q, now, "removed"))
            s.delete(q)
            out["removed"] += 1
            touched.add(key)
    skipped = sum(1 for v in values if mapping.get(v.source_key) is None)
    return out | {"values": len(values), "unmapped": skipped}, touched


def _now_resolving(s: Session, up: Upstream, source: str, mapping: dict[str, int | None]) -> list[str]:
    """Keys recorded as unmapped that secmaster-svc now knows; they go into mapping."""
    keys = sorted(s.scalars(select(UnmappedKey.source_key).where(UnmappedKey.source == source)))
    if not keys:
        return []
    found, unknown = up.resolve(SCHEMES.get(source, source), keys)
    mapping.update(found)
    mapping.update(dict.fromkeys(unknown))
    return sorted(found)


def _exclusions(s: Session) -> dict[int, list[tuple[str, date, date]]]:
    """GOLDEN_EXCLUDE by sec_id (through instrument_ref's short names)."""
    ids = {r.short_name: r.sec_id for r in s.scalars(select(InstrumentRef))}
    return {ids[name]: [(src, lo, hi) for src, lo, hi, _ in windows]
            for name, windows in GOLDEN_EXCLUDE.items() if name in ids}


def _usable(source: str, key: tuple, exclude: dict) -> bool:
    return not any(src == source and lo <= key[1] <= hi for src, lo, hi in exclude.get(key[0], ()))


def _refresh_golden(s: Session, keys: set, now: datetime, exclude: dict | None = None) -> dict:
    """Recompute the golden value of every touched key, a batch of dates at a time."""
    exclude = exclude or {}
    out = {"golden_set": 0, "golden_removed": 0}
    keys = {k for k in keys if k[2] in PRIORITY}
    dates = sorted({k[1] for k in keys})
    for i in range(0, len(dates), GOLDEN_BATCH):
        chunk = dates[i:i + GOLDEN_BATCH]
        in_chunk = set(chunk)
        quotes: dict[tuple, dict[str, Quote]] = {}
        for q in s.scalars(select(Quote).where(Quote.as_of.in_(chunk))):
            quotes.setdefault((q.sec_id, q.as_of, q.field), {})[q.source] = q
        golden = {(g.sec_id, g.as_of, g.field): g for g in s.scalars(select(Golden).where(Golden.as_of.in_(chunk)))}
        for key in sorted(k for k in keys if k[1] in in_chunk):
            by_source = quotes.get(key, {})
            best = next((by_source[src] for src in PRIORITY[key[2]]
                         if src in by_source and _usable(src, key, exclude)), None)
            g = golden.get(key)
            if best is None:
                if g is not None:
                    s.delete(g)
                    out["golden_removed"] += 1
            elif g is None:
                s.add(Golden(sec_id=key[0], as_of=key[1], field=key[2], value=best.value, source=best.source,
                             updated_at=now))
                out["golden_set"] += 1
            elif (g.value, g.source) != (best.value, best.source):
                g.value, g.source, g.updated_at = best.value, best.source, now
                out["golden_set"] += 1
        s.flush()
    return out


def _refresh_names(s: Session, up: Upstream, now: datetime) -> int:
    infos = up.instruments()
    for sec_id, i in infos.items():
        row = s.get(InstrumentRef, sec_id)
        if row is None:
            s.add(InstrumentRef(sec_id=sec_id, short_name=i.short_name, type=i.type, status=i.status,
                                refreshed_at=now))
        else:
            row.short_name, row.type, row.status, row.refreshed_at = i.short_name, i.type, i.status, now
    return len(infos)


def _load(s: Session, up: Upstream, now: datetime, progress: dict) -> dict:
    """Each month is its own transaction: its quotes, the golden values it touched and its watermark.

    A full-history load (the backfill) is hundreds of thousands of quotes, so
    the session is cleared after every month to keep memory flat. A failure
    keeps the months already committed; the rest have no new watermark, so
    the next load picks them up.
    """
    progress["instruments"] = _refresh_names(s, up, now)
    s.commit()
    exclude = _exclusions(s)
    golden = {"golden_set": 0, "golden_removed": 0}
    progress |= golden
    for source in SOURCES:
        marks = dict(s.execute(select(SourcePeriod.period, SourcePeriod.capture_id)
                               .where(SourcePeriod.source == source)).all())
        periods = up.list_periods(source)
        mapping: dict[str, int | None] = {}
        resolving = _now_resolving(s, up, source, mapping)
        if resolving:
            # Periods that skipped values may hold these keys: forget their watermarks.
            for (period,) in s.execute(select(SourcePeriod.period).where(
                    SourcePeriod.source == source, SourcePeriod.unmapped > 0)).all():
                marks.pop(period, None)
        todo = [p for p in periods if marks.get(p.period) != p.latest_capture_id]
        unmapped: dict[str, int] = {}
        totals = {"added": 0, "revised": 0, "removed": 0, "values": 0}
        entry = {"source": source, "periods": len(periods), "reloaded": 0, "now_resolving": len(resolving),
                 "unmapped": [], **totals}
        progress["sources"].append(entry)
        for p in todo:
            out, keys = _load_period(s, up, source, p.period, mapping, unmapped, now)
            s.flush()
            for k, v in _refresh_golden(s, keys, now, exclude).items():
                progress[k] += v
            mark = s.get(SourcePeriod, (source, p.period))
            if mark is None:
                s.add(SourcePeriod(source=source, period=p.period, capture_id=p.latest_capture_id,
                                   values=out["values"], unmapped=out["unmapped"], loaded_at=now))
            else:
                mark.capture_id, mark.values, mark.unmapped, mark.loaded_at = (
                    p.latest_capture_id, out["values"], out["unmapped"], now)
            s.commit()
            s.expunge_all()
            for k in totals:
                entry[k] += out[k]
            entry["reloaded"] += 1
        for key, n in unmapped.items():
            row = s.get(UnmappedKey, (source, key))
            if row is None:
                s.add(UnmappedKey(source=source, source_key=key, first_seen_at=now, last_seen_at=now, values=n))
            else:
                row.last_seen_at, row.values = now, n
        # A key that now resolves isn't unmapped any more.
        mapped_now = [k for k, v in mapping.items() if v is not None]
        if mapped_now:
            s.execute(delete(UnmappedKey).where(UnmappedKey.source == source, UnmappedKey.source_key.in_(mapped_now)))
        s.commit()
        entry["unmapped"] = sorted(unmapped)[:50]
        entry["unmapped_keys"] = len(unmapped)
    # Re-apply the exclusion windows, so a change to GOLDEN_EXCLUDE reaches golden values already set.
    windows = {(q.sec_id, q.as_of, q.field) for sec_id, ws in exclude.items() for _, lo, hi in ws
               for q in s.scalars(select(Quote).where(Quote.sec_id == sec_id, Quote.as_of >= lo, Quote.as_of <= hi))}
    for k, v in _refresh_golden(s, windows, now, exclude).items():
        progress[k] += v
    s.commit()
    return progress


def run_load(s: Session, up: Upstream) -> dict:
    """Load whatever changed upstream, a month per transaction. Raises LoadError (recorded) on failure."""
    started = datetime.now(UTC)
    progress: dict = {"sources": []}
    try:
        out = _load(s, up, started, progress)
    except Exception as e:  # recorded, then reported to Airflow as a failure
        s.rollback()
        detail = f"{type(e).__name__}: {e}"[:1500]
        s.add(LoadRun(started_at=started, finished_at=datetime.now(UTC), outcome="error",
                      detail=json.dumps({"error": detail, "before_it": progress})[:4000]))
        s.commit()
        raise LoadError(detail) from e
    s.add(LoadRun(started_at=started, finished_at=datetime.now(UTC), outcome="ok", detail=json.dumps(out)))
    s.commit()
    return out


def run_rebuild(s: Session, up: Upstream, source: str = "") -> dict:
    """Forget the watermarks (for one source, or all) and load: every month is re-read and corrected."""
    if source and source not in SOURCES:
        raise LoadError(f"unknown source {source!r}; known: {SOURCES}")
    q = delete(SourcePeriod)
    if source:
        q = q.where(SourcePeriod.source == source)
    s.execute(q)
    return run_load(s, up)
