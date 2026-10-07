"""Are FedInvest's end-of-day prices believable? (mkt-data's docs/phase-3.md, step 7.)

The price freshness check (app/freshness.py) only asks whether a price is
there. This asks whether a day's prices look like a real day, by comparing
each security's golden price with its previous one (the latest earlier day
with prices):

- **Unchanged:** the share of securities whose price didn't move. Bills
  accrete every day and coupon securities' prices are quoted to six
  decimals, so a real day moves nearly all of them; a page that repeats the
  previous day's file leaves most unchanged. Flagged as stale above
  UNCHANGED_LIMIT, when at least MIN_COMPARED securities could be compared.
- **Jumps:** a security whose price moved more than its type plausibly can
  in a day (MAX_MOVE, per 100): a misread column or a shifted decimal.

`check` compares the latest priced day on or before a date (the metrics use
the prices' due date) for active securities. `history` runs the same
comparison over every priced day in a range, for setting the limits against
real history: how many days were stale, how big real moves get per type.
"""

import heapq
import itertools
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Golden, InstrumentRef

FIELD = "price"
# The largest believable one-day move per type, per 100 of face.
MAX_MOVE = {
    "ust_bill": Decimal("0.5"),
    "ust_frn": Decimal("0.5"),
    "ust_note": Decimal(5),
    "ust_tips": Decimal(15),
    "ust_bond": Decimal(12),
}
DEFAULT_MAX_MOVE = Decimal(15)
UNCHANGED_LIMIT = Decimal("0.8")
MIN_COMPARED = 50
# Absolute moves per 100 counted per type in `history`: how many moves were larger than each.
EDGES = tuple(Decimal(x) for x in ("0.05", "0.1", "0.25", "0.5", "1", "2", "3", "4", "5", "6", "8", "10", "15"))
HISTORY_TOP = 10
GAP_DAYS = 4  # more calendar days than this between priced days is a gap (a Friday to Tuesday holiday is 4)
HISTORY_DAYS_LISTED = 50  # the days with the most unchanged, and the days with jumps, each


def _num(v: Decimal) -> str:
    return format(v.normalize(), "f")


def limit_for(security_type: str) -> Decimal:
    return MAX_MOVE.get(security_type, DEFAULT_MAX_MOVE)


def compare_days(today: dict[int, Decimal], before: dict[int, Decimal], types: dict[int, str]) -> dict:
    """One day's prices against the previous ones, by sec_id (pure: no database)."""
    common = sorted(set(today) & set(before))
    unchanged = sum(1 for i in common if today[i] == before[i])
    ratio = (Decimal(unchanged) / len(common)).quantize(Decimal("0.0001")) if common else Decimal(0)
    jumps = []
    for i in common:
        change = today[i] - before[i]
        t = types.get(i, "")
        if abs(change) > limit_for(t):
            jumps.append({"sec_id": i, "type": t, "before": before[i], "after": today[i], "change": change})
    jumps.sort(key=lambda j: abs(j["change"]) / limit_for(j["type"]), reverse=True)
    return {"compared": len(common), "unchanged": unchanged, "unchanged_ratio": ratio,
            "stale": len(common) >= MIN_COMPARED and ratio > UNCHANGED_LIMIT, "jumps": jumps}


def _prices(s: Session, day: date) -> dict[int, Decimal]:
    return dict(s.execute(
        select(Golden.sec_id, Golden.value)
        .join(InstrumentRef, InstrumentRef.sec_id == Golden.sec_id)
        .where(Golden.as_of == day, Golden.field == FIELD, InstrumentRef.status == "active")
    ).all())


def _latest_day(s: Session, on_or_before: date, strictly_before: bool = False) -> date | None:
    cond = Golden.as_of < on_or_before if strictly_before else Golden.as_of <= on_or_before
    return s.scalar(select(func.max(Golden.as_of)).where(Golden.field == FIELD, cond))


def check(s: Session, on_or_before: date) -> dict:
    """The latest priced day on or before a date, against the priced day before it, for active securities."""
    day = _latest_day(s, on_or_before)
    before = _latest_day(s, day, strictly_before=True) if day else None
    if day is None or before is None:
        return {"day": day, "before": before, "compared": 0, "unchanged": 0, "unchanged_ratio": Decimal(0),
                "stale": False, "jumps": []}
    types = dict(s.execute(select(InstrumentRef.sec_id, InstrumentRef.type)).all())
    return {"day": day, "before": before, **compare_days(_prices(s, day), _prices(s, before), types)}


def history(s: Session, start: date, end: date) -> dict:
    """Every priced day from start to end against the one before it, every security (matured or not).

    Streams the golden prices in date order, so only two days are in memory at once.
    """
    refs = {i: (n, t) for i, n, t in s.execute(select(InstrumentRef.sec_id, InstrumentRef.short_name,
                                                      InstrumentRef.type))}
    types = {i: t for i, (_, t) in refs.items()}
    first = _latest_day(s, start, strictly_before=True) or start
    rows = s.execute(
        select(Golden.as_of, Golden.sec_id, Golden.value)
        .where(Golden.field == FIELD, Golden.as_of >= first, Golden.as_of <= end)
        .order_by(Golden.as_of)
        .execution_options(yield_per=10_000)
    )
    days = stale = 0
    ratios = {"over_0.1": 0, "over_0.25": 0, "over_0.5": 0}
    most_unchanged: list = []  # heap of (ratio, day, before, compared)
    jump_days: list[dict] = []
    gaps: list[dict] = []
    moves: dict[str, dict] = {}
    top: dict[str, list] = {}
    prev: dict[int, Decimal] | None = None
    prev_day = None
    for day, group in itertools.groupby(rows, key=lambda r: r[0]):
        today = {r[1]: r[2] for r in group}
        if prev is not None and day >= start:
            r = compare_days(today, prev, types)
            days += 1
            stale += r["stale"]
            for k, lim in (("over_0.1", "0.1"), ("over_0.25", "0.25"), ("over_0.5", "0.5")):
                ratios[k] += r["unchanged_ratio"] > Decimal(lim)
            if (day - prev_day).days > GAP_DAYS:
                gaps.append({"after": prev_day.isoformat(), "next": day.isoformat(),
                             "weekdays_missing": sum(1 for n in range(1, (day - prev_day).days)
                                                     if date.fromordinal(prev_day.toordinal() + n).weekday() < 5)})
            entry = (r["unchanged_ratio"], day.isoformat(), prev_day.isoformat(), r["compared"])
            if len(most_unchanged) < HISTORY_DAYS_LISTED:
                heapq.heappush(most_unchanged, entry)
            elif entry > most_unchanged[0]:
                heapq.heapreplace(most_unchanged, entry)
            if r["jumps"] and len(jump_days) < HISTORY_DAYS_LISTED:
                worst = r["jumps"][0]
                jump_days.append({"day": day.isoformat(), "before": prev_day.isoformat(), "jumps": len(r["jumps"]),
                                  "worst": refs.get(worst["sec_id"], (str(worst["sec_id"]), ""))[0],
                                  "change": _num(worst["change"])})
            for i in set(today) & set(prev):
                t = types.get(i, "") or "unknown"
                change = today[i] - prev[i]
                size = abs(change)
                m = moves.setdefault(t, {"moves": 0, "over_limit": 0, "larger_than": {str(e): 0 for e in EDGES}})
                m["moves"] += 1
                m["over_limit"] += size > limit_for(t)
                for e in EDGES:
                    if size <= e:
                        break
                    m["larger_than"][str(e)] += 1
                heap = top.setdefault(t, [])
                item = (size, day.isoformat(), i, _num(prev[i]), _num(today[i]))
                if len(heap) < HISTORY_TOP:
                    heapq.heappush(heap, item)
                elif size > heap[0][0]:
                    heapq.heapreplace(heap, item)
        prev, prev_day = today, day
    largest = {
        t: [{"day": d, "security": refs.get(i, (str(i), ""))[0], "before": b, "after": a,
             "change": _num(Decimal(a) - Decimal(b))}
            for _, d, i, b, a in sorted(h, reverse=True)]
        for t, h in sorted(top.items())
    }
    return {"start": start.isoformat(), "end": end.isoformat(), "days": days, "stale_days": stale,
            "unchanged_ratio_days": ratios,
            "most_unchanged": [{"day": d, "before": b, "compared": n, "unchanged_ratio": str(x)}
                               for x, d, b, n in sorted(most_unchanged, reverse=True)],
            "jump_days": jump_days,
            "gaps": gaps, "gap_weekdays": sum(g["weekdays_missing"] for g in gaps),
            "limits": {t: str(v) for t, v in MAX_MOVE.items()}, "moves": dict(sorted(moves.items())),
            "largest": largest}
