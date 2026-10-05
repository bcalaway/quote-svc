"""Job API for Airflow (ADR-0031 in nyc_pa_aws_gitops), plus read-only lookups.

Airflow's DAGs call these over the home-platform network; the work runs in
this container. Every endpoint requires `Authorization: Bearer
<AIRFLOW_TOKEN>` (other apps share the network), is idempotent (Airflow
retries) and answers with a JSON summary that shows in the task log.

The GET endpoints only read, and also accept READ_TOKEN. They're the same
reads as the gRPC API, by short name, for checking by hand and for home-mcp
(phase 2, step B10).
"""

import hmac
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app import db, quotes
from app.config import settings
from app.load import LoadError, run_load, run_rebuild
from app.upstream import GrpcUpstream

router = APIRouter(prefix="/jobs")


def require_token(authorization: str | None = Header(default=None)) -> None:
    if not settings.airflow_token:
        raise HTTPException(503, "job API disabled: AIRFLOW_TOKEN isn't set")
    given = (authorization or "").removeprefix("Bearer ").strip().encode()
    if not hmac.compare_digest(given, settings.airflow_token.encode()):
        raise HTTPException(401, "bad or missing job token")


def require_read_token(authorization: str | None = Header(default=None)) -> None:
    """The Airflow token, or the read-only token. For GET endpoints only."""
    tokens = [t for t in (settings.airflow_token, settings.read_token) if t]
    if not tokens:
        raise HTTPException(503, "job API disabled: no AIRFLOW_TOKEN or READ_TOKEN set")
    given = (authorization or "").removeprefix("Bearer ").strip().encode()
    # Compare against every token (no early exit), so timing doesn't say which matched.
    matches = [hmac.compare_digest(given, t.encode()) for t in tokens]
    if not any(matches):
        raise HTTPException(401, "bad or missing token")


def _upstream() -> GrpcUpstream:
    return GrpcUpstream(settings.mkt_data_grpc, settings.secmaster_grpc)


@router.post("/load", dependencies=[Depends(require_token)])
def load() -> dict:
    """Load every month mkt-data has new or revised values for. 502 on failure."""
    try:
        with db.session() as s, _upstream() as up:
            return run_load(s, up)
    except LoadError as e:
        raise HTTPException(502, f"load failed: {e}") from None


@router.post("/rebuild", dependencies=[Depends(require_token)])
def rebuild(source: str = "") -> dict:
    """Re-read every month (of one source, or all) and correct the quotes to match."""
    try:
        with db.session() as s, _upstream() as up:
            return run_rebuild(s, up, source.upper())
    except LoadError as e:
        raise HTTPException(502, f"rebuild failed: {e}") from None


def _ids(s, names: list[str]) -> list[int]:
    try:
        return quotes.sec_ids_for(s, names)
    except quotes.UnknownInstrument as e:
        raise HTTPException(404, str(e)) from None


@router.get("/series", dependencies=[Depends(require_read_token)])
def series(name: Annotated[list[str], Query()], start: date, end: date, source: str = "") -> dict:
    with db.session() as s:
        return {"series": quotes.series(s, _ids(s, name), start, end, source=source.upper())}


@router.get("/curve", dependencies=[Depends(require_read_token)])
def curve(name: Annotated[list[str], Query()], as_of: date | None = None) -> dict:
    with db.session() as s:
        return quotes.curve(s, _ids(s, name), as_of)


@router.get("/compare", dependencies=[Depends(require_read_token)])
def compare(name: Annotated[list[str], Query()], start: date, end: date, only_differences: bool = False) -> dict:
    with db.session() as s:
        return {"rows": quotes.compare(s, _ids(s, name), start, end, only_differences=only_differences)}


@router.get("/latest", dependencies=[Depends(require_read_token)])
def latest(name: Annotated[list[str], Query()]) -> dict:
    with db.session() as s:
        return {"latest": quotes.latest(s, _ids(s, name))}
