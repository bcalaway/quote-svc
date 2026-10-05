"""Is today's Treasury curve in, and is any series stuck? (mkt-data's docs/phase-2.md, Part B step 8.)

Computed on each /metrics scrape, so it keeps answering when loads stop
(that's when it matters):

- **Due date:** the latest SIFMA-US business day whose curve is overdue. The
  curve for business day D is due by 9:00 a.m. New York on the next calendar
  day; Treasury usually posts it by 6 p.m. on D. Holidays and weekends come
  from calendar-svc (cached for an hour), so a holiday never makes a curve
  due. If calendar-svc can't answer, weekdays stand in and
  `quote_svc_curve_calendar_ok` is 0.
- **Missing:** an active instrument (one with a UST-PAR value in the last
  ACTIVE_DAYS) whose latest UST-PAR value is older than the due date.
- **Repeats:** how many of an active instrument's latest golden values in a
  row are identical: a feed stuck on yesterday's numbers looks like this.
"""

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Golden, Quote

NEW_YORK = ZoneInfo("America/New_York")
CALENDAR = "SIFMA-US"
SOURCE = "UST-PAR"
DUE_HOUR = 9  # the next day, New York
ACTIVE_DAYS = 30
REPEAT_LOOKBACK = 30  # golden values read per instrument for the repeat count
CACHE_SECONDS = 3600
CLOSES_WINDOW = 21  # days back from today the calendar is asked about

_cache: dict = {}


def due_date(now: datetime, closed: set[date]) -> date:
    """The latest business day whose curve deadline (the next day at 09:00 New York) has passed."""
    local = now.astimezone(NEW_YORK)
    d = local.date() - timedelta(days=1 if local.hour >= DUE_HOUR else 2)
    while d.weekday() >= 5 or d in closed:
        d -= timedelta(days=1)
    return d


def closed_days(calendars, today: date) -> tuple[set[date], bool]:
    """SIFMA-US closes in the last few weeks, from calendar-svc (cached); (set(), False) if it can't answer."""
    hit = _cache.get(today)
    if hit and time.monotonic() - hit[2] < CACHE_SECONDS:
        return hit[0], hit[1]
    try:
        with calendars() as cal:
            closed, ok = cal.closed_days(CALENDAR, today - timedelta(days=CLOSES_WINDOW), today), True
    except Exception:  # noqa: BLE001 -- unreachable or erroring: weekdays stand in, and the metric says so
        closed, ok = set(), False
    _cache.clear()
    _cache[today] = (closed, ok, time.monotonic())
    return closed, ok


def repeats(values: list) -> int:
    """How many of the latest values (newest first) are the same as the newest."""
    n = 0
    for v in values:
        if v != values[0]:
            break
        n += 1
    return n


def check(s: Session, calendars, now: datetime | None = None) -> dict:
    now = now or datetime.now(NEW_YORK)
    today = now.astimezone(NEW_YORK).date()
    closed, ok = closed_days(calendars, today)
    due = due_date(now, closed)
    since = due - timedelta(days=ACTIVE_DAYS)
    last = dict(s.execute(
        select(Quote.sec_id, func.max(Quote.as_of))
        .where(Quote.source == SOURCE, Quote.field == "yield", Quote.as_of >= since).group_by(Quote.sec_id)
    ).all())
    repeat = {}
    for sec_id in last:
        values = list(s.scalars(
            select(Golden.value).where(Golden.sec_id == sec_id, Golden.field == "yield")
            .order_by(Golden.as_of.desc()).limit(REPEAT_LOOKBACK)
        ))
        repeat[sec_id] = repeats(values)
    return {"due": due, "calendar_ok": ok, "last": last, "missing": {i: d < due for i, d in last.items()},
            "repeats": repeat}
