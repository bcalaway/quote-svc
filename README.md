# quote-svc

The market data platform's quote store: golden quotes per instrument, date and field, with source priority, revisions and history. It loads mkt-data's near-raw observations over gRPC (`mkt-data:9090`, `Observations`), maps each source key to an instrument through secmaster-svc, and converts percent to decimal. The plan and status live in mkt-data's [docs/phase-2.md](https://github.com/bcalaway/mkt-data/blob/main/docs/phase-2.md) (Part B, step B5).

It runs on the home platform's AWS hub (`bcalaway/nyc_pa_aws_gitops`) as a registry app (`apps/registry.yml`: own Postgres database, Airflow pipelines, no Authentik client, no previews). Started from `templates/python` there, whose README explains the template's pieces; [docs/app-platform.md](https://github.com/bcalaway/nyc_pa_aws_gitops/blob/main/docs/app-platform.md) is the platform contract. The template's `Item` model, `/db-check`, `/login` and `ExampleService.Ping` are still examples until step B5 replaces them.

## How it runs

- **Container:** one process with HTTP on 8000 and gRPC on 9090, internal only: no Traefik route and no DNS record. Other services reach it on the `home-platform` network as `quote-svc:8000` / `quote-svc:9090`.
- **Database:** `quote-svc` on the hub's Postgres 16 (role, database and password created by the platform, `/home-platform/postgres/quote-svc-password`). Schema changes are Alembic migrations, applied when the container starts.
- **CI/CD:** `ci.yml` runs the platform's `app-ci.yml` on every PR (`ci / Build, test, lint` is required on `main`). `cd.yml` builds and pushes to ECR on merge, then deploys to the hub. Docs-only merges don't deploy.
- **Secrets:** anything under `/home-platform/quote-svc/` in SSM arrives in the container's environment at deploy time (the Airflow job token arrives as `AIRFLOW_TOKEN`).

## Local development

```
pip install -r requirements.txt -r requirements-dev.txt
./gen_proto.sh
uvicorn app.main:app --reload
```

`POSTGRES_PASSWORD` is optional locally; without it the app runs with no database.

Tests and lint: `pytest` and `ruff check app/ tests/ migrations/`, or `docker build --target test .` / `--target lint .`, which is what CI runs. Where PyPI is blocked, `scripts/sandbox-test.sh` runs everything but `tests/test_grpc.py`.
