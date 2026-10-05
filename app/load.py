"""The load job: mkt-data's near-raw observations into quotes and golden values (phase 2, B5).

For each source, in priority order:

1. List its months from mkt-data, each with `latest_capture_id`, the newest
   capture any of its values came from. A month whose capture id differs
   from the watermark here (or is new) is reloaded; the rest are skipped. A
   revision or a dropped value can only come from a newer capture of that
   month, so it always moves the id.
2. Read the month's current values and map each source key to an instrument
   through secmaster-svc (one batch per source and load). Keys with no
   instrument are recorded in `unmapped_key` and skipped.
3. Convert to decimals (`percent` / 100, exact) and diff against the month's
   quotes: a new value is inserted; a changed one moves the old row to
   `quote_history` as revised; a value the source no longer has moves there
   as removed.

Then golden values are recomputed for every (sec_id, date, field) touched:
the highest-priority source with a value wins (UST-PAR, then H15-TCM; H.15
fills the years before 1990 and is a cross-check after).

Idempotent: a second run with nothing new reads only the month lists. A
rebuild clears the watermarks, so every month is re-read and corrected.
"""

import json
from calendar import monthrange
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Golden, InstrumentRef, LoadRun, Quote, QuoteHistory, SourcePeriod, UnmappedKey
from app.upstream import Upstream

# Source priority per field, highest first.
PRIORITY = {"yield": ["UST-PAR", "H15-TCM"]}
SOURCES = ["UST-PAR", "H15-TCM"]
# Dates per query when recomputing golden values (a backfill touches decades).
GOLDEN_BATCH = 500
# Near-raw units, and how to turn one into a decimal.
UNITS = {"percent": Decimal(100)}


class LoadError(RuntimeError):
    pass


def _month(period: str) -> tuple[date, date]:
    y, m = (int(x) for x in period.split("-"))
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def _to_decimal(value: str, unit: str) -> Decimal:
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
        found, unknown = up.resolve(source, new_keys)
        mapping.update(found)
        mapping.update(dict.fromkeys(unknown))
    want: dict[tuple, tuple] = {}
    for v in values:
        sec_id = mapping.get(v.source_key)
        if sec_id is None:
            unmapped[v.source_key] = unmapped.get(v.source_key, 0) + 1
            continue
        key = (sec_id, date.fromisoformat(v.as_of), v.field)
        if key in want:
            raise LoadError(f"{source} {period}: two values for {v.source_key} on {v.as_of} ({v.field})")
        want[key] = (_to_decimal(v.value, v.unit), v.observation_id, v.capture_id)

    first, last = _month(period)
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
    return out | {"values": len(values)}, touched


def _refresh_golden(s: Session, keys: set, now: datetime) -> dict:
    """Recompute the golden value of every touched key, a batch of dates at a time."""
    out = {"golden_set": 0, "golden_removed": 0}
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
            best = next((by_source[src] for src in PRIORITY.get(key[2], SOURCES) if src in by_source), None)
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
    names = up.instruments()
    for sec_id, name in names.items():
        row = s.get(InstrumentRef, sec_id)
        if row is None:
            s.add(InstrumentRef(sec_id=sec_id, short_name=name, refreshed_at=now))
        else:
            row.short_name, row.refreshed_at = name, now
    return len(names)


def _load(s: Session, up: Upstream, now: datetime) -> dict:
    summary = {"instruments": _refresh_names(s, up, now), "sources": []}
    touched: set = set()
    for source in SOURCES:
        marks = {p.period: p for p in s.scalars(select(SourcePeriod).where(SourcePeriod.source == source))}
        periods = up.list_periods(source)
        todo = [p for p in periods if marks.get(p.period) is None or marks[p.period].capture_id != p.latest_capture_id]
        mapping: dict[str, int | None] = {}
        unmapped: dict[str, int] = {}
        totals = {"added": 0, "revised": 0, "removed": 0, "values": 0}
        for p in todo:
            out, keys = _load_period(s, up, source, p.period, mapping, unmapped, now)
            for k in totals:
                totals[k] += out[k]
            touched |= keys
            mark = marks.get(p.period)
            if mark is None:
                s.add(SourcePeriod(source=source, period=p.period, capture_id=p.latest_capture_id,
                                   values=out["values"], loaded_at=now))
            else:
                mark.capture_id, mark.values, mark.loaded_at = p.latest_capture_id, out["values"], now
            s.flush()
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
        summary["sources"].append({
            "source": source, "periods": len(periods), "reloaded": len(todo),
            "unmapped": sorted(unmapped), **totals,
        })
    s.flush()
    summary |= _refresh_golden(s, touched, now)
    return summary


def run_load(s: Session, up: Upstream) -> dict:
    """Load whatever changed upstream. Commits; raises LoadError (recorded) on failure."""
    started = datetime.now(UTC)
    try:
        out = _load(s, up, started)
    except Exception as e:  # recorded, then reported to Airflow as a failure
        s.rollback()
        detail = f"{type(e).__name__}: {e}"[:2000]
        s.add(LoadRun(started_at=started, finished_at=datetime.now(UTC), outcome="error", detail=detail))
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
