"""Reads for other services (proto/quotes.proto) and the job API.

Plain functions returning plain dicts, tested without gRPC. Values are
canonical decimal strings (rates as decimals: "0.041" = 4.10%), never floats.
Answers carry sec_ids plus the short names quote-svc keeps from
secmaster-svc's last load; it never calls secmaster-svc per request.
"""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.load import SOURCES
from app.models import Golden, InstrumentRef, Quote


class UnknownInstrument(LookupError):
    pass


def _s(v) -> str:
    """One canonical decimal string: no trailing zeros, no exponent ("0.041", not "0.0410" or "4.1E-2").

    Postgres keeps whatever scale the division left, and SQLite pads to ten
    places; consumers shouldn't see either.
    """
    return format(v.normalize(), "f")


def names(s: Session) -> dict[int, str]:
    return dict(s.execute(select(InstrumentRef.sec_id, InstrumentRef.short_name)).all())


def sec_ids_for(s: Session, wanted: list[str]) -> list[int]:
    """Short names (case-insensitive) or sec_ids as strings, to sec_ids. UnknownInstrument for any miss."""
    by_name = {n.upper(): i for i, n in names(s).items()}
    out, missing = [], []
    for w in wanted:
        w = w.strip()
        if w.isdigit():
            out.append(int(w))
        elif w.upper() in by_name:
            out.append(by_name[w.upper()])
        else:
            missing.append(w)
    if missing:
        raise UnknownInstrument(f"no instrument named {', '.join(missing)}")
    return out


def series(s: Session, sec_ids: list[int], start: date, end: date, field: str = "yield", source: str = "") -> list[dict]:
    """Each instrument's values from start to end: golden by default, or one source's."""
    nm = names(s)
    out = []
    for sec_id in sec_ids:
        if source:
            rows = s.execute(select(Quote.as_of, Quote.value, Quote.source).where(
                Quote.sec_id == sec_id, Quote.field == field, Quote.source == source,
                Quote.as_of >= start, Quote.as_of <= end).order_by(Quote.as_of)).all()
        else:
            rows = s.execute(select(Golden.as_of, Golden.value, Golden.source).where(
                Golden.sec_id == sec_id, Golden.field == field,
                Golden.as_of >= start, Golden.as_of <= end).order_by(Golden.as_of)).all()
        out.append({"sec_id": sec_id, "short_name": nm.get(sec_id, ""),
                    "points": [{"as_of": d.isoformat(), "value": _s(v), "source": src} for d, v, src in rows]})
    return out


def curve(s: Session, sec_ids: list[int], as_of: date | None = None, field: str = "yield") -> dict:
    """Golden values for these instruments on one date (default: the latest date any of them has)."""
    if as_of is None:
        as_of = s.scalar(select(func.max(Golden.as_of)).where(Golden.sec_id.in_(sec_ids), Golden.field == field))
    nm = names(s)
    if as_of is None:
        return {"as_of": "", "points": [], "missing": [nm.get(i, str(i)) for i in sec_ids]}
    rows = {g.sec_id: g for g in s.scalars(select(Golden).where(
        Golden.sec_id.in_(sec_ids), Golden.as_of == as_of, Golden.field == field))}
    return {
        "as_of": as_of.isoformat(),
        "points": [{"sec_id": i, "short_name": nm.get(i, ""), "value": _s(rows[i].value), "source": rows[i].source}
                   for i in sec_ids if i in rows],
        "missing": [nm.get(i, str(i)) for i in sec_ids if i not in rows],
    }


def compare(s: Session, sec_ids: list[int], start: date, end: date, field: str = "yield",
            only_differences: bool = False) -> list[dict]:
    """Every source's value per instrument and date, and whether they differ."""
    nm = names(s)
    rows: dict[tuple, dict] = {}
    for q in s.scalars(select(Quote).where(
            Quote.sec_id.in_(sec_ids), Quote.field == field, Quote.as_of >= start, Quote.as_of <= end)):
        rows.setdefault((q.sec_id, q.as_of), {})[q.source] = q.value
    out = []
    for (sec_id, as_of), by_source in sorted(rows.items()):
        differs = len(set(by_source.values())) > 1
        if only_differences and not differs:
            continue
        out.append({
            "sec_id": sec_id, "short_name": nm.get(sec_id, ""), "as_of": as_of.isoformat(), "differs": differs,
            "values": [{"source": src, "value": _s(by_source[src])} for src in SOURCES if src in by_source],
        })
    return out


def latest(s: Session, sec_ids: list[int], field: str = "yield") -> list[dict]:
    """Each instrument's most recent golden value."""
    nm = names(s)
    out = []
    for sec_id in sec_ids:
        g = s.scalars(select(Golden).where(Golden.sec_id == sec_id, Golden.field == field)
                      .order_by(Golden.as_of.desc()).limit(1)).first()
        out.append({"sec_id": sec_id, "short_name": nm.get(sec_id, ""), "as_of": g.as_of.isoformat() if g else "",
                    "value": _s(g.value) if g else "", "source": g.source if g else ""})
    return out
