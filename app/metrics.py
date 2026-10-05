"""Prometheus metrics (GET /metrics): the load job and the quote store's contents.

Text format, computed from the database on each scrape. Prometheus scrapes it
on the home-platform network as quote-svc:8000 (nyc_pa_aws_gitops's
prometheus.yml); no auth, like the other scrape targets, and nothing in it is
sensitive. Instruments are labelled by short name (from secmaster-svc's last
load). The missing-business-day check against SIFMA-US and the alert rules
come with step B8.
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Response
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from app import coverage as coverage_mod
from app import db
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

router = APIRouter()

# Dates and gaps listed per instrument or series in the detail metrics.
DETAIL_LIMIT = 20


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


def render(s) -> str:
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
    out.metric("quote_svc_source_periods", "gauge", "Months loaded, by source.", [({"source": k}, n) for k, n in sorted(periods)])
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
    for u in unmapped:
        per_source[u.source] = per_source.get(u.source, 0) + 1
    out.metric("quote_svc_unmapped_keys", "gauge",
               "Source keys secmaster-svc has no instrument for (their values aren't loaded), by source.",
               [({"source": k}, n) for k, n in sorted(per_source.items())])
    out.metric("quote_svc_unmapped_key", "gauge", "1 for each unmapped source key, with its values in the last months read.",
               [({"source": u.source, "key": u.source_key}, u.values) for u in unmapped])
    return out.text()


@router.get("/metrics")
def metrics() -> Response:
    try:
        with db.session() as s:
            body = render(s)
    except (db.DatabaseNotConfigured, SQLAlchemyError):
        body = "# HELP quote_svc_up 1 when the database answered this scrape.\n# TYPE quote_svc_up gauge\nquote_svc_up 0\n"
    return Response(body, media_type="text/plain; version=0.0.4")
