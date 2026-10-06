"""Reads for other services (proto/quotes.proto) and the job API.

Plain functions returning plain dicts, tested without gRPC. Values are
canonical decimal strings (rates as decimals: "0.041" = 4.10%), never floats.
Answers carry sec_ids plus the short names quote-svc keeps from
secmaster-svc's last load; it never calls secmaster-svc per request.
"""

from datetime import date

from sqlalchemy import Date, Numeric, case, cast, func, literal_column, select, type_coerce
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


INTERVALS = ("day", "week", "month", "quarter", "year")


def _period(dialect: str, col, interval: str):
    """SQL for the first calendar day of a date's period: its Monday, or the 1st of its month, quarter or year."""
    if interval == "day":
        return col
    if dialect == "postgresql":
        return cast(func.date_trunc(interval, col), Date)
    # SQLite (tests): dates are ISO text.
    if interval == "week":
        return func.date(col, "-6 days", "weekday 1")
    if interval == "month":
        return func.strftime("%Y-%m-01", col)
    if interval == "year":
        return func.strftime("%Y-01-01", col)
    month = cast(func.strftime("%m", col), Numeric)
    first = case((month <= 3, "01"), (month <= 6, "04"), (month <= 9, "07"), else_="10")
    return func.strftime("%Y-", col) + first + literal_column("'-01'")


def bars(s: Session, sec_ids: list[int], start: date, end: date, interval: str, field: str = "yield",
         source: str = "") -> list[dict]:
    """Each instrument's values summed up per period: open, high, low and close, the close's date and source.

    Done in the database: per period it returns only the first and last rows (window functions), with the
    period's high and low, so a monthly view of 64 years reads about 780 rows per instrument, not 16,000.
    """
    stmt = bars_statement(s.get_bind().dialect.name, sec_ids, start, end, interval, field, source)
    rows = s.execute(stmt).all()
    nm = names(s)
    by_id: dict[int, list[dict]] = {i: [] for i in sec_ids}
    for r in rows:
        period = r.p if isinstance(r.p, str) else r.p.isoformat()
        out = by_id[r.sec_id]
        if r.first == 1:
            out.append({"start": period, "open": _s(r.value), "high": _s(r.high), "low": _s(r.low)})
        if r.last == 1:
            out[-1] |= {"last": r.as_of.isoformat() if hasattr(r.as_of, "isoformat") else str(r.as_of),
                        "close": _s(r.value), "source": r.source}
    return [{"sec_id": i, "short_name": nm.get(i, ""), "bars": by_id[i]} for i in sec_ids]


def bars_statement(dialect: str, sec_ids: list[int], start: date, end: date, interval: str, field: str = "yield",
                   source: str = ""):
    """Per (instrument, period): the first and last rows, each with the period's high and low."""
    if interval not in INTERVALS:
        raise ValueError(f"interval {interval!r} isn't one of {', '.join(INTERVALS)}")
    t = Quote if source else Golden
    p = _period(dialect, t.as_of, interval).label("p")
    part = (t.sec_id, _period(dialect, t.as_of, interval))
    where = [t.sec_id.in_(sec_ids), t.field == field, t.as_of >= start, t.as_of <= end]
    if source:
        where.append(Quote.source == source)
    inner = select(
        t.sec_id, t.as_of, t.value, t.source, p,
        func.row_number().over(partition_by=part, order_by=t.as_of).label("first"),
        func.row_number().over(partition_by=part, order_by=t.as_of.desc()).label("last"),
        type_coerce(func.max(t.value).over(partition_by=part), Numeric).label("high"),
        type_coerce(func.min(t.value).over(partition_by=part), Numeric).label("low"),
    ).where(*where).subquery()
    return select(inner).where((inner.c.first == 1) | (inner.c.last == 1)).order_by(inner.c.sec_id, inner.c.as_of)


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
