"""quote-svc: load new and revised Treasury CMT yields, Treasury prices, fixings and positioning from mkt-data.

Runs whenever mkt-data marks the Asset `mkt_data_cmt_observations` (after
each successful CMT capture), `mkt_data_treasury_prices` (after a
FedInvest capture or rebuild that added, changed or removed prices), or
`mkt_data_fixings` / `mkt_data_positioning` (after a fixings or CFTC capture
that changed observations), so a
new day's curve or prices reach the golden quotes within minutes, and
nightly as a catch-up in case an event was missed. The work runs in the quote-svc container
(POST /jobs/load), which re-reads only months whose newest capture changed;
this DAG only calls it (ADR-0031 in nyc_pa_aws_gitops). A failed run
retries, and Grafana's "Airflow task failed" alert fires if retries run out.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.sdk import Asset, AssetOrTimeSchedule, CronTriggerTimetable, dag, task

# The platform's helper lives at Airflow's DAG root (home_platform_jobs.py);
# this repo's dags/ is delivered to dags/quote-svc/ there, so the root is
# one level up. Airflow normally has it on sys.path; this makes sure of it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from home_platform_jobs import call_app_job

# Marked by mkt-data's mkt_data__treasury_cmt_capture after each successful run.
CMT_OBSERVATIONS = Asset("mkt_data_cmt_observations")
# Marked by mkt-data's mkt_data__treasury_securities_capture and _rebuild when TD-PRICES changed.
TREASURY_PRICES = Asset("mkt_data_treasury_prices")
# Marked by mkt-data's mkt_data__futures_sources_capture when a fixing or a CFTC report changed (phase 4, step 6).
FIXINGS = Asset("mkt_data_fixings")
POSITIONING = Asset("mkt_data_positioning")


@dag(
    dag_id="quote_svc__load",
    schedule=AssetOrTimeSchedule(
        timetable=CronTriggerTimetable("13 7 * * *", timezone="UTC"),  # nightly catch-up, 07:13 UTC
        assets=CMT_OBSERVATIONS | TREASURY_PRICES | FIXINGS | POSITIONING,
    ),
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=2),  # a rebuild after the backfill reads every month since 1962
    default_args={"retries": 3, "retry_delay": timedelta(minutes=10)},
    tags=["quote-svc", "quotes", "treasury", "fixings", "cftc"],
    doc_md=__doc__,
)
def quote_load():
    @task
    def load() -> dict:
        return call_app_job("quote-svc", "load", timeout=3600)

    load()


quote_load()
