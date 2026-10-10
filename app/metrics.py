"""Prometheus metrics (GET /metrics): the load job and the quote store's contents.

Text format, computed from the database on each scrape. Prometheus scrapes it
on the home-platform network as quote-svc:8000 (nyc_pa_aws_gitops's
prometheus.yml); no auth, like the other scrape targets, and nothing in it is
sensitive. Instruments are labelled by short name (from secmaster-svc's last
load). Whether the latest Treasury curve is in on time, and whether a series
is stuck repeating itself, come from app/freshness.py (step B8); the alert
rules are in nyc_pa_aws_gitops.
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Response
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from app import coverage as coverage_mod
from app import db, freshness, load, sanity
from app.config import settings
from app.models import (
    CoverageGap,
    CoverageSeries,
    Golden,
    InstrumentRef,
    LoadRun,
    Quote,
    QuoteHistory,
    SourcePeriod,
    UnmappedKey,
)
from app.upstream import GrpcCalendars

router = APIRouter()

# Dates and gaps listed per instrument or series in the detail metrics.
DETAIL_LIMIT = 20
# Unmapped keys listed per source (TD-PRICES has hundreds until secmaster-svc's backfill).
UNMAPPED_LIMIT = 50


def _num(v) -> str:
    return format(v.normalize(), "f")


def _epoch(t: datetime) -> float:
    # Postgres returns aware timestamps; SQLite (tests) returns naive UTC ones.
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()


def _escape(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Out:
    def __init__(self):
        self.lines: list[str] = []

    def metric(self, name: str, kind: str, help_: str, samples: list[tuple[dict, float]]) -> None:
        self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
        for labels, value in samples:
            body = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
            num = int(value) if float(value).is_integer() else value
            self.lines.append(f"{name}{{{body}}} {num}" if body else f"{name} {num}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _day_epoch(d) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()


def _calendars() -> GrpcCalendars:
    # Short: this runs inside a Prometheus scrape (10 s timeout), and a failure is cached for an hour.
    return GrpcCalendars(settings.calendar_grpc, timeout=3)


def _freshness(s, out: _Out, name, calendars) -> None:
    f = freshness.check(s, calendars)
    out.metric("quote_svc_curve_due_date_timestamp_seconds", "gauge",
               "The latest SIFMA-US business day whose Treasury curve is due (by 9:00 a.m. New York the next day).",
               [({}, _day_epoch(f["due"]))])
    out.metric("quote_svc_curve_calendar_ok", "gauge",
               "0 if calendar-svc couldn't say which days were closed (weekdays stand in).", [({}, int(f["calendar_ok"]))])
    out.metric("quote_svc_curve_active_instruments", "gauge",
               f"Instruments with a UST-PAR value in the {freshness.ACTIVE_DAYS} days before the due date.",
               [({}, len(f["last"]))])
    out.metric("quote_svc_curve_last_date_timestamp_seconds", "gauge", "Each active instrument's latest UST-PAR date.",
               [({"instrument": name(i)}, _day_epoch(d)) for i, d in sorted(f["last"].items())])
    out.metric("quote_svc_curve_missing", "gauge", "1 if an active instrument has no UST-PAR value for the due date.",
               [({"instrument": name(i)}, int(m)) for i, m in sorted(f["missing"].items())])
    out.metric("quote_svc_golden_repeat_days", "gauge",
               f"How many of an active instrument's latest golden yields in a row are identical (of the last "
               f"{freshness.REPEAT_LOOKBACK}).",
               [({"instrument": name(i)}, n) for i, n in sorted(f["repeats"].items())])


def _prices_freshness(s, out: _Out, name, calendars) -> None:
    f = freshness.check_prices(s, calendars)
    out.metric("quote_svc_prices_due_date_timestamp_seconds", "gauge",
               "The latest SIFMA-US business day whose Treasury end-of-day prices are due (FedInvest prints them the "
               "next business day evening; due by 9:00 a.m. New York the day after).", [({}, _day_epoch(f["due"]))])
    out.metric("quote_svc_prices_outstanding", "gauge",
               f"Active Treasury securities with a TD-PRICES price in the {freshness.PRICES_ACTIVE_DAYS} days before "
               "the due date.", [({}, len(f["last"]))])
    out.metric("quote_svc_prices_missing_count", "gauge",
               "Outstanding Treasury securities with no TD-PRICES price for the due date.", [({}, len(f["missing"]))])
    out.metric("quote_svc_prices_missing", "gauge",
               f"1 for each outstanding security with no price for the due date (the first {DETAIL_LIMIT}).",
               [({"instrument": name(i)}, 1) for i in f["missing"][:DETAIL_LIMIT]])
    _prices_sanity(s, out, name, f["due"])


def _prices_sanity(s, out: _Out, name, due) -> None:
    c = sanity.check(s, due)
    out.metric("quote_svc_prices_sanity_date_timestamp_seconds", "gauge",
               "The latest day with Treasury end-of-day prices on or before their due date: the day the price "
               "sanity metrics describe.", [({}, _day_epoch(c["day"]))] if c["day"] else [])
    out.metric("quote_svc_prices_compared", "gauge",
               "Active Treasury securities with a price on that day and the priced day before.",
               [({}, c["compared"])])
    out.metric("quote_svc_prices_unchanged_ratio", "gauge",
               "The share of those whose price didn't move: near 0 on a real day; a repeated page leaves most "
               f"unchanged (stale above {sanity.UNCHANGED_LIMIT} with at least {sanity.MIN_COMPARED} compared).",
               [({}, _num(c["unchanged_ratio"]))])
    out.metric("quote_svc_prices_stale", "gauge", "1 if that day's prices look like a repeat of the day before.",
               [({}, int(c["stale"]))])
    out.metric("quote_svc_prices_jumps_count", "gauge",
               "Securities whose price moved more than their type plausibly can in a day (app/sanity.py MAX_MOVE).",
               [({}, len(c["jumps"]))])
    out.metric("quote_svc_prices_jump", "gauge",
               f"Each such move, per 100 (the first {DETAIL_LIMIT}, largest against its limit first).",
               [({"instrument": name(j["sec_id"]), "type": j["type"]}, _num(j["change"])) for j in c["jumps"][:DETAIL_LIMIT]])


def render(s, calendars=None) -> str:
    out = _Out()
    names = dict(s.execute(select(InstrumentRef.sec_id, InstrumentRef.short_name)).all())

    def name(sec_id):
        return names.get(sec_id, str(sec_id))

    out.metric("quote_svc_up", "gauge", "1 when the database answered this scrape.", [({}, 1)])
    last_ok = s.scalar(select(func.max(LoadRun.finished_at)).where(LoadRun.outcome == "ok"))
    latest_run = s.scalars(select(LoadRun).order_by(LoadRun.id.desc()).limit(1)).first()
    out.metric("quote_svc_load_last_success_timestamp_seconds", "gauge",
               "When the last successful load from mkt-data finished.", [({}, _epoch(last_ok))] if last_ok else [])
    out.metric("quote_svc_load_ok", "gauge", "1 if the latest load succeeded, 0 if it failed.",
               [({}, int(latest_run.outcome == "ok"))] if latest_run else [])

    by_source = s.execute(select(Quote.source, func.count()).group_by(Quote.source)).all()
    out.metric("quote_svc_quotes", "gauge", "Current quotes, by source.", [({"source": k}, n) for k, n in sorted(by_source)])
    periods = s.execute(select(SourcePeriod.source, func.count()).group_by(SourcePeriod.source)).all()
    out.metric("quote_svc_source_periods", "gauge", "Periods loaded (months; days for TD-PRICES), by source.", [({"source": k}, n) for k, n in sorted(periods)])
    history = s.execute(select(QuoteHistory.source, QuoteHistory.reason, func.count())
                        .group_by(QuoteHistory.source, QuoteHistory.reason)).all()
    out.metric("quote_svc_superseded_quotes", "gauge", "Earlier values kept in quote_history, by source and reason.",
               [({"source": k, "reason": r}, n) for k, r, n in sorted(history)])

    golden = s.execute(select(Golden.sec_id, Golden.source, func.count(), func.min(Golden.as_of), func.max(Golden.as_of))
                       .where(Golden.field == "yield").group_by(Golden.sec_id, Golden.source)).all()
    counts, first, last = [], {}, {}
    for sec_id, source, n, lo, hi in golden:
        counts.append(({"instrument": name(sec_id), "source": source}, n))
        first[sec_id] = min(first.get(sec_id, lo), lo)
        last[sec_id] = max(last.get(sec_id, hi), hi)
    out.metric("quote_svc_golden_values", "gauge", "Golden yields, by instrument and the source that won.", sorted(counts, key=str))
    out.metric("quote_svc_golden_first_date_timestamp_seconds", "gauge", "Each instrument's first golden date.",
               [({"instrument": name(i)}, _day_epoch(d)) for i, d in sorted(first.items())])
    out.metric("quote_svc_golden_last_date_timestamp_seconds", "gauge", "Each instrument's latest golden date.",
               [({"instrument": name(i)}, _day_epoch(d)) for i, d in sorted(last.items())])

    a, b = aliased(Quote), aliased(Quote)
    differ = s.execute(
        select(a.sec_id, func.count()).join(b, (a.sec_id == b.sec_id) & (a.as_of == b.as_of) & (a.field == b.field))
        .where(a.source == "UST-PAR", b.source == "H15-TCM", a.value != b.value).group_by(a.sec_id)
    ).all()
    out.metric("quote_svc_source_disagreements", "gauge",
               "Dates where UST-PAR and H15-TCM give different values, by instrument.",
               [({"instrument": name(i)}, n) for i, n in sorted(differ)])
    detail = s.execute(
        select(a.sec_id, a.as_of, a.value, b.value)
        .join(b, (a.sec_id == b.sec_id) & (a.as_of == b.as_of) & (a.field == b.field))
        .where(a.source == "UST-PAR", b.source == "H15-TCM", a.value != b.value)
        .order_by(a.sec_id, a.as_of.desc())
    ).all()
    shown: dict[int, int] = {}
    samples = []
    for sec_id, as_of, ust, h15 in detail:
        if shown.get(sec_id, 0) >= DETAIL_LIMIT:
            continue
        shown[sec_id] = shown.get(sec_id, 0) + 1
        samples.append(({"instrument": name(sec_id), "date": as_of.isoformat(), "ust_par": _num(ust), "h15_tcm": _num(h15)},
                        float((ust - h15) * 10000)))
    out.metric("quote_svc_source_disagreement_bp", "gauge",
               f"UST-PAR minus H15-TCM in basis points, per disagreeing date (the latest {DETAIL_LIMIT} per instrument).",
               samples)

    cov = list(s.scalars(select(CoverageSeries)))
    lab = [({"instrument": name(c.sec_id), "series": c.series}, c) for c in cov]
    out.metric("quote_svc_coverage_first_date_timestamp_seconds", "gauge", "Each series' first date.",
               [(lb, _day_epoch(c.first_date)) for lb, c in lab])
    out.metric("quote_svc_coverage_last_date_timestamp_seconds", "gauge", "Each series' latest date.",
               [(lb, _day_epoch(c.last_date)) for lb, c in lab])
    out.metric("quote_svc_coverage_values", "gauge", "Values in each series (golden, UST-PAR, H15-TCM).",
               [(lb, c.values) for lb, c in lab])
    out.metric("quote_svc_coverage_missing_days", "gauge",
               "Business days between a series' first and last date that it has no value for.",
               [(lb, c.missing_days) for lb, c in lab])
    out.metric("quote_svc_coverage_closed_day_values", "gauge", "Values on days the market was closed.",
               [(lb, c.closed_day_values) for lb, c in lab])
    out.metric("quote_svc_coverage_basis", "gauge", "1, labelled with the calendars that set each series' business days.",
               [(lb | {"basis": c.basis}, 1) for lb, c in lab])
    out.metric("quote_svc_coverage_closed_day", "gauge",
               f"1 for each date a series has a value though the market was closed (the first {DETAIL_LIMIT} per series).",
               [(lb | {"date": d}, 1) for lb, c in lab for d in json.loads(c.closed_days or "[]")[:DETAIL_LIMIT]])
    gaps = list(s.scalars(select(CoverageGap).order_by(CoverageGap.sec_id, CoverageGap.series, CoverageGap.days.desc())))
    shown_gaps: dict[tuple, int] = {}
    gap_samples = []
    for g in gaps:
        k = (g.sec_id, g.series)
        if shown_gaps.get(k, 0) >= DETAIL_LIMIT:
            continue
        shown_gaps[k] = shown_gaps.get(k, 0) + 1
        gap_samples.append(({"instrument": name(g.sec_id), "series": g.series, "start": g.start_date.isoformat(),
                             "end": g.end_date.isoformat()}, g.days))
    out.metric("quote_svc_coverage_gap_days", "gauge",
               f"Business days in each gap (the longest {DETAIL_LIMIT} per series).", gap_samples)
    refreshed = max((c.refreshed_at for c in cov), default=None)
    out.metric("quote_svc_coverage_refreshed_timestamp_seconds", "gauge", "When coverage was last recomputed.",
               [({}, _epoch(refreshed))] if refreshed else [])
    out.metric("quote_svc_coverage_ok", "gauge", "0 if the last coverage refresh failed (calendar-svc unreachable).",
               [({}, 0 if coverage_mod.last_failure() else 1)])

    unmapped = list(s.scalars(select(UnmappedKey).order_by(UnmappedKey.source, UnmappedKey.source_key)))
    per_source: dict[str, int] = {}
    expected: dict[str, int] = {}
    for u in unmapped:
        bucket = expected if u.source in load.UNMAPPED_EXPECTED else per_source
        bucket[u.source] = bucket.get(u.source, 0) + 1
    out.metric("quote_svc_unmapped_keys", "gauge",
               "Source keys secmaster-svc has no instrument for (their values aren't loaded), by source.",
               [({"source": k}, n) for k, n in sorted(per_source.items())])
    out.metric("quote_svc_out_of_scope_keys", "gauge",
               "Keys of sources that report markets beyond the security master (the CFTC's equity, crypto and "
               "volatility markets), not loaded and not alerted on, by source.",
               [({"source": k}, n) for k, n in sorted(expected.items())])
    shown: dict[str, int] = {}
    key_samples = []
    for u in unmapped:
        if shown.get(u.source, 0) < UNMAPPED_LIMIT:
            shown[u.source] = shown.get(u.source, 0) + 1
            key_samples.append(({"source": u.source, "key": u.source_key}, u.values))
    out.metric("quote_svc_unmapped_key", "gauge",
               f"Each unmapped source key (the first {UNMAPPED_LIMIT} per source), with its values in the last periods read.",
               key_samples)
    _freshness(s, out, name, calendars or _calendars)
    _prices_freshness(s, out, name, calendars or _calendars)
    return out.text()


@router.get("/metrics")
def metrics() -> Response:
    try:
        with db.session() as s:
            body = render(s)
    except (db.DatabaseNotConfigured, SQLAlchemyError):
        body = "# HELP quote_svc_up 1 when the database answered this scrape.\n# TYPE quote_svc_up gauge\nquote_svc_up 0\n"
    return Response(body, media_type="text/plain; version=0.0.4")
