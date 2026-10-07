"""quote-svc: Treasury price sanity over history (mkt-data's docs/phase-3.md, step 7).

Manual. Compares every day of FedInvest end-of-day prices from `start` to
`end` with the priced day before it (POST /jobs/prices/sanity-history): how
many days look like a repeated page, and how large real one-day moves get
per security type, with the largest of each. For setting app/sanity.py's
limits, which the /metrics price sanity check and its alert use. Read-only:
it changes nothing.

Trigger it from the Airflow UI or home-mcp's airflow_trigger; the defaults
cover all of FedInvest's history.
"""

import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from airflow.sdk import Param, dag, get_current_context, task

# The platform's helper lives at Airflow's DAG root (see load.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from home_platform_jobs import call_app_job


@dag(
    dag_id="quote_svc__price_sanity_history",
    schedule=None,
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=1),
    params={
        "start": Param("2008-01-01", type="string", description="YYYY-MM-DD"),
        "end": Param("2099-12-31", type="string", description="YYYY-MM-DD"),
    },
    tags=["quote-svc", "treasury", "prices"],
    doc_md=__doc__,
)
def price_sanity_history():
    @task
    def run() -> dict:
        p = get_current_context()["params"]
        start, end = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
        r = call_app_job("quote-svc", f"prices/sanity-history?start={start}&end={end}", timeout=1800)
        print(f"{r['days']} days compared, {r['stale_days']} stale; unchanged ratio: {r['unchanged_ratio_days']}")
        for d in r["most_unchanged"]:
            print(f"unchanged: {d}")
        for d in r["jump_days"]:
            print(f"jumps: {d}")
        for t, m in r["moves"].items():
            print(f"moves {t}: {m}")
        for t, rows in r["largest"].items():
            for row in rows:
                print(f"largest {t}: {row}")
        return r

    run()


price_sanity_history()
