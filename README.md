# quote-svc

The market data platform's quote store: golden quotes per instrument, date and field, with source priority, revisions and history. It loads mkt-data's near-raw observations over gRPC (`mkt-data:9090`, `Observations`), maps each source key to an instrument through secmaster-svc, and converts percent to decimal. The plan and status live in mkt-data's [docs/phase-2.md](https://github.com/bcalaway/mkt-data/blob/main/docs/phase-2.md) (Part B, step B5).

It runs on the home platform's AWS hub (`bcalaway/nyc_pa_aws_gitops`) as a registry app (`apps/registry.yml`: own Postgres database, Airflow pipelines, no Authentik client, no previews). Started from `templates/python` there, whose README explains the template's pieces; [docs/app-platform.md](https://github.com/bcalaway/nyc_pa_aws_gitops/blob/main/docs/app-platform.md) is the platform contract.

## How it runs

- **Container:** one process with HTTP on 8000 and gRPC on 9090, internal only: no Traefik route and no DNS record. Other services reach it on the `home-platform` network as `quote-svc:8000` / `quote-svc:9090`.
- **Database:** `quote-svc` on the hub's Postgres 16 (role, database and password created by the platform, `/home-platform/postgres/quote-svc-password`). Schema changes are Alembic migrations, applied when the container starts.
- **CI/CD:** `ci.yml` runs the platform's `app-ci.yml` on every PR (`ci / Build, test, lint` is required on `main`). `cd.yml` builds and pushes to ECR on merge, then deploys to the hub. Docs-only merges don't deploy.
- **Secrets:** anything under `/home-platform/quote-svc/` in SSM arrives in the container's environment at deploy time (the Airflow job token arrives as `AIRFLOW_TOKEN`).

## Data model

`app/models.py`, migration 0002. Values are `numeric`, rates as decimals (`0.0425` = 4.25%).

- **`quote`:** one source's current value per `(sec_id, source, as_of, field)`, with `observation_id` and `capture_id` back to mkt-data's near-raw row and raw capture.
- **`quote_history`:** earlier values, `revised` (the source changed it) or `removed` (the source dropped it), with when they were superseded.
- **`golden`:** the value to use per `(sec_id, as_of, field)` and which source it came from. The priority for `yield` is UST-PAR, then H15-TCM, so H.15 fills the years before 1990 and is a cross-check after.
- **`source_period`:** watermarks, the newest mkt-data capture each source's month was loaded from.
- **`unmapped_key`:** source keys secmaster-svc has no instrument for (Treasury's `BC_30YEARDISPLAY`, for one); their values aren't loaded.
- **`instrument_ref`:** secmaster-svc's short names, refreshed every load, for answers, logs and metrics.
- **`load_run`:** each load and what it did.

## The load

`POST /jobs/load` (`app/load.py`), run by `quote_svc__load` whenever mkt-data marks the Asset `mkt_data_cmt_observations` and nightly at 07:13 UTC:

1. For each source, list its months from mkt-data (`Observations.ListPeriods`) and re-read only those whose `latest_capture_id` moved past the watermark. A revision or dropped value always comes from a newer capture of that month.
2. Map each source key to a `sec_id` through secmaster-svc (`Securities.Resolve`, once per key per load) and convert `percent` to a decimal exactly.
3. Diff against the month's quotes: insert new values, move changed ones to history as `revised`, and dropped ones as `removed`.
4. Recompute golden values for every key touched.

It's idempotent, and a failure rolls back and is recorded. `POST /jobs/rebuild?source=` forgets the watermarks (for one source or all) and loads, so every month is re-read and corrected from near-raw.

## APIs

**gRPC** (`proto/quotes.proto`, `quote-svc:9090`), service `quote_svc.Quotes`:
- `GetSeries`: golden values by default, or one source's.
- `GetCurve`: one date, defaulting to the latest; lists which instruments are missing.
- `CompareSources`: every source's value per date, flagging where they differ.
- `GetLatest`.

Requests take `sec_id`s; answers add the cached short names. Values are canonical decimal strings ("0.041" = 4.10%). quote-svc is a client of `proto/observations.proto` and `proto/securities.proto`, copies of mkt-data's and secmaster-svc's; keep them in step.

**Job API** (bearer `AIRFLOW_TOKEN`; the GETs also take `READ_TOKEN`): `POST /jobs/load`, `POST /jobs/rebuild`, and `GET /jobs/series`, `/jobs/curve`, `/jobs/compare`, `/jobs/latest` by short name (`?name=UST-10Y-CMT`).

**Metrics** (`GET /metrics`, scraped as `quote-svc:8000`):
- Load: `quote_svc_load_ok` and `quote_svc_load_last_success_timestamp_seconds`.
- Store: quotes and months loaded by source, superseded quotes by reason.
- Golden values by instrument and winning source, and each instrument's first and last golden date.
- `quote_svc_source_disagreements{instrument}`, where UST-PAR and H.15 differ.
- Unmapped keys.

The missing-business-day check against SIFMA-US and the alert rules are step B8.

## Local development

```
pip install -r requirements.txt -r requirements-dev.txt
./gen_proto.sh
uvicorn app.main:app --reload
```

`POSTGRES_PASSWORD` is optional locally; without it the app runs with no database.

Tests and lint: `pytest` and `ruff check app/ tests/ migrations/`, or `docker build --target test .` / `--target lint .`, which is what CI runs. Where PyPI is blocked, `scripts/sandbox-test.sh` runs everything but `tests/test_grpc.py`.
